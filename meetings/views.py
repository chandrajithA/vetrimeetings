from django.contrib import messages
from django.conf import settings
from django.contrib.auth.decorators import login_required
from django.shortcuts import render, get_object_or_404, redirect
from django.http import JsonResponse
from django.views.decorators.http import require_POST
from livekit.api import AccessToken, VideoGrants, LiveKitAPI
from livekit.api import DeleteRoomRequest, ListParticipantsRequest
import asyncio
import secrets
from .models import Meeting, MeetingRecording
import json
from livekit.api import UpdateRoomMetadataRequest
from .emails import send_meeting_invite
from .models import *
from django.utils.dateparse import parse_datetime
from django.utils import timezone
import threading
from django.views.decorators.csrf import csrf_exempt
from django.contrib.auth import update_session_auth_hash
from django.contrib.auth.hashers import check_password
from django.http import HttpResponse
import requests



######################################################
# HELPERS
######################################################

def _build_meeting_url(request, room_name):
    domain = getattr(settings, "DOMAIN", None)
    if domain:
        scheme = "https" if getattr(settings, "SECURE_SSL_REDIRECT", False) or \
                            request.META.get("HTTP_X_FORWARDED_PROTO") == "https" else "http"
        base = f"{scheme}://{domain.rstrip('/')}"
    else:
        base = request.build_absolute_uri("/").rstrip("/")
    return f"{base}/room/{room_name}/"


######################################################
# DASHBOARD
######################################################

def dashboard(request):
    return render(request, "meetings/dashboard.html")


def _ensure_meeting_chat(meeting, user):
    chat, _ = MeetingChat.objects.get_or_create(meeting=meeting)
    MeetingChatMember.objects.get_or_create(chat=chat, user=user)
    return chat



######################################################
# TOKEN
######################################################

@login_required
def get_token(request, room_name):
    meeting, created = Meeting.objects.get_or_create(
        room_name=room_name,
        defaults={
            'host': request.user,
            'title': f"Meeting {room_name}",
        }
    )
    if not meeting.meeting_url:
        meeting.meeting_url = _build_meeting_url(request, room_name)
        meeting.save(update_fields=["meeting_url"])

    # Allow the lobby preview to override the display name for this session
    display_name = request.GET.get("display_name", "").strip() or request.user.name

    token = AccessToken(
        api_key=settings.LIVEKIT_API_KEY,
        api_secret=settings.LIVEKIT_API_SECRET,
    )
    token.with_identity(str(request.user.id))
    token.with_name(display_name)          # ← uses lobby name if provided
    token.with_grants(VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    ))

    return JsonResponse({
        "token": token.to_jwt(),
        "livekit_url": settings.LIVEKIT_URL,
        "room_name": room_name,
        "user_name": display_name,
        "is_host": meeting.host == request.user,
    })


######################################################
# ROOM VIEW
######################################################

@login_required
def room(request, room_name):
    meeting = Meeting.objects.filter(room_name=room_name).first()

    if not meeting:
        messages.error(request, "Error in joining meeting. Meeting not found.")
        return redirect('meetings:dashboard')

    if not meeting.meeting_url:
        meeting.meeting_url = _build_meeting_url(request, room_name)
        meeting.save(update_fields=["meeting_url"])

    if meeting.host != request.user and not meeting.is_active:
        messages.error(request, "This meeting has ended.")
        return redirect('meetings:dashboard')

    if meeting.host == request.user and not meeting.is_active:
        meeting.is_active = True
        meeting.save(update_fields=["is_active"])

    # Ensure the meeting has a persistent chat and this user is a member
    chat = _ensure_meeting_chat(meeting, request.user)

    return render(request, "meetings/room.html", {
        "meeting":     meeting,
        "room_name":   room_name,
        "is_host":     meeting.host == request.user,
        "meeting_url": meeting.meeting_url,
        "chat_id":     chat.id,
    })
    
    



######################################################
# CREATE MEETING
######################################################

@login_required
def create_meeting(request):
    title = request.GET.get("title") or request.POST.get("title", "Quick Meeting")
    room_name = secrets.token_hex(32)

    meeting = Meeting.objects.create(
        host=request.user,
        room_name=room_name,
        title=title,
    )
    meeting.meeting_url = _build_meeting_url(request, room_name)
    meeting.save(update_fields=["meeting_url"])
    
    # Create persistent chat for instant meetings too
    _ensure_meeting_chat(meeting, request.user)

    redirect_url = f"/room/{room_name}/"

    # AJAX POST from dashboard → return JSON
    if request.method == "POST":
        return JsonResponse({"redirect": redirect_url, "room_name": room_name})

    # Direct GET (fallback / legacy) → plain redirect
    return redirect('meetings:meeting_room', room_name=room_name)


######################################################
# JOIN BY MEETING ID + PASSCODE
# POST /meeting/join/  { meeting_id, passcode }
# Returns JSON { room_name } on success, or { error } on failure.
######################################################

@login_required
@require_POST
def join_by_id(request):
    """
    Validate meeting_id + passcode and redirect to the room.
    Accepts both POST (form/AJAX) and GET (for direct URL use).
    """
        
    try:
        body = json.loads(request.body)
    except Exception:
        body = request.POST

    meeting_id = str(body.get("meeting_id", "")).strip().replace(" ", "").replace("-", "")
    passcode   = str(body.get("passcode", "")).strip().upper()

    if not meeting_id or not passcode:
        return JsonResponse({"error": "Meeting ID and passcode are required."}, status=400)

    try:
        meeting = Meeting.objects.get(meeting_id=meeting_id, is_active=True)
    except Meeting.DoesNotExist:
        return JsonResponse({"error": "Meeting not found. Check your Meeting ID."}, status=404)

    if meeting.passcode.upper() != passcode:
        return JsonResponse({"error": "Incorrect passcode."}, status=403)

    return JsonResponse({"room_name": meeting.room_name, "redirect": f"/room/{meeting.room_name}/"})


######################################################
# END MEETING
######################################################

@login_required
@require_POST
def end_meeting(request, room_name):
    meeting = Meeting.objects.filter(room_name=room_name, host=request.user).first()
    if not meeting:
        return JsonResponse({"error": "Meeting not found"}, status=404)

    # Mark inactive immediately
    meeting.is_active = False
    meeting.save(update_fields=["is_active"])

    async def _end_livekit():
        import asyncio as _asyncio
        async with LiveKitAPI(
            url=settings.LIVEKIT_URL,
            api_key=settings.LIVEKIT_API_KEY,
            api_secret=settings.LIVEKIT_API_SECRET,
        ) as lk:
            try:
                await lk.room.update_room_metadata(
                    UpdateRoomMetadataRequest(room=room_name, metadata="ended")
                )
            except Exception as e:
                pass  # silently ignore — clients already got the signal

            await _asyncio.sleep(0.5)

            try:
                await lk.room.delete_room(DeleteRoomRequest(room=room_name))
            except Exception:
                pass  # room may already be gone

    def _run_in_thread():
        """
        Run async LiveKit calls in a brand-new event loop on a worker thread.
        Completely isolated from the ASGI event loop, so no CancelledError leaks.
        We also suppress the asyncio CancelledError log that asgiref emits when
        the parent request is torn down before the thread finishes.
        """
        import logging
        import asyncio as _asyncio

        # Silence the asgiref 'CancelledError exception in shielded future' noise
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        logging.getLogger("asgiref").setLevel(logging.CRITICAL)

        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_end_livekit())
        except Exception:
            pass  # swallow everything — meeting is already marked inactive in DB
        finally:
            try:
                # Cancel any lingering tasks cleanly before closing the loop
                pending = _asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(
                        _asyncio.gather(*pending, return_exceptions=True)
                    )
            except Exception:
                pass
            loop.close()

    t = threading.Thread(target=_run_in_thread, daemon=True)
    t.start()
    t.join(timeout=6)

    return JsonResponse({"status": "ended"})


######################################################
# PARTICIPANT LIST
######################################################

@login_required
def participants(request, room_name):
    get_object_or_404(Meeting, room_name=room_name)

    async def _list():
        async with LiveKitAPI(
            url=settings.LIVEKIT_URL,
            api_key=settings.LIVEKIT_API_KEY,
            api_secret=settings.LIVEKIT_API_SECRET,
        ) as lk:
            res = await lk.room.list_participants(ListParticipantsRequest(room=room_name))
            return [{"identity": p.identity, "name": p.name, "joined_at": p.joined_at} for p in res.participants]

    return JsonResponse({"participants": asyncio.run(_list())})


######################################################
# SAVE RECORDING
######################################################

@login_required
def save_recording(request):
    if request.method != "POST":
        return JsonResponse({"error": "POST only"}, status=405)

    room_name = request.POST.get("room_name")
    file      = request.FILES.get("recording")
    duration  = int(request.POST.get('duration_seconds', 0))

    if not room_name or not file:
        return JsonResponse({"error": "Missing data"}, status=400)

    try:
        meeting = Meeting.objects.get(room_name=room_name)
        rec = MeetingRecording.objects.create(
            meeting=meeting, file=file, duration_seconds=duration
        )
        return JsonResponse({"url": rec.file.url})
    except Meeting.DoesNotExist:
        return JsonResponse({"error": "Meeting not found"}, status=404)
    
    
@login_required
def recordings_hub(request):
    # Recordings from meetings hosted by this user
    recordings = MeetingRecording.objects.filter(
        meeting__host=request.user
    ).select_related('meeting').order_by('-recorded_at')
    return render(request, 'meetings/recordings_hub.html', {'recordings': recordings})


@login_required
@require_POST
def delete_recording(request, recording_id):
    try:
        rec = get_object_or_404(MeetingRecording, id=recording_id, meeting__host=request.user)
        if rec.file:
            rec.file.delete(save=False)
        if hasattr(rec, 'thumbnail') and rec.thumbnail:
            rec.thumbnail.delete(save=False)
        rec.delete()
        return JsonResponse({'status': 'deleted'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'error': str(e)}, status=500)

    



######################################################
# SCHEDULE MEETING
######################################################

@login_required
@require_POST
def schedule_meeting(request):
    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    title        = body.get("title", "Scheduled Meeting").strip() or "Scheduled Meeting"
    start_str    = body.get("start")       # ISO string or None
    end_str      = body.get("end")
    is_all_day   = body.get("all_day", False)
    repeat       = body.get("repeat", "none")
    repeat_end   = body.get("repeat_end")  # date string YYYY-MM-DD or None
    invitee_emails = [e.strip().lower() for e in body.get("invitees", []) if e.strip()]

    room_name = secrets.token_hex(32)
    meeting = Meeting.objects.create(
        host=request.user,
        room_name=room_name,
        title=title,
        is_scheduled=True,
        is_all_day=is_all_day,
        repeat=repeat,
    )

    if start_str:
        dt = parse_datetime(start_str)
        meeting.scheduled_start = dt if dt.tzinfo else timezone.make_aware(dt)
    if end_str:
        dt = parse_datetime(end_str)
        meeting.scheduled_end = dt if dt.tzinfo else timezone.make_aware(dt)
    if repeat_end:
        from datetime import date
        meeting.repeat_end_date = date.fromisoformat(repeat_end)

    meeting.meeting_url = _build_meeting_url(request, room_name)
    meeting.save()

    # Save invitees
    for email in invitee_emails:
        MeetingInvitee.objects.get_or_create(meeting=meeting, email=email)

    # Create group chat for this meeting
    chat = MeetingChat.objects.create(meeting=meeting)
    # Add host
    MeetingChatMember.objects.create(chat=chat, user=request.user)
    # Add registered invitees (match by email)
    from django.contrib.auth import get_user_model
    User = get_user_model()
    for email in invitee_emails:
        try:
            u = User.objects.get(email=email)
            MeetingChatMember.objects.get_or_create(chat=chat, user=u)
        except User.DoesNotExist:
            pass  # external — will join when they register/click link

    # Send invite emails
    send_meeting_invite(meeting, invitee_emails)

    return JsonResponse({
        "status": "scheduled",
        "meeting_id": meeting.meeting_id,
        "passcode": meeting.passcode,
        "room_name": room_name,
        "meeting_url": meeting.meeting_url,
    })


######################################################
# CHAT VIEWS
######################################################

@login_required
def chat_list(request):
    """Return all chats the user is a member of (not deleted for self)."""
    memberships = MeetingChatMember.objects.filter(
        user=request.user,
        deleted_for_self=False,
        is_removed=False,
    ).select_related('chat__meeting')

    chats = []
    for m in memberships:
        last = m.chat.messages.order_by('-sent_at').first()
        chats.append({
            "chat_id":      m.chat.id,
            "meeting_title": m.chat.meeting.title,
            "meeting_url":  m.chat.meeting.meeting_url,
            "last_message": last.text if last else "",
            "last_at":      last.sent_at.isoformat() if last else "",
        })

    return JsonResponse({"chats": chats})


@login_required
def chat_messages(request, chat_id):
    chat = get_object_or_404(MeetingChat, id=chat_id)

    # Make sure user is a member
    member = MeetingChatMember.objects.filter(
        chat=chat, user=request.user, is_removed=False
    ).first()
    if not member:
        return JsonResponse({"error": "Not a member"}, status=403)

    messages = chat.messages.exclude(
        deleted_by=request.user
    ).order_by('sent_at').values(
        'id', 'sender__name', 'sender_id', 'text', 'sent_at'
    )

    members = MeetingChatMember.objects.filter(
        chat=chat, is_removed=False
    ).select_related('user')

    return JsonResponse({
        "messages": [
            {
                "id":        m['id'],
                "sender":    m['sender__name'],
                "is_me":     m['sender_id'] == request.user.id,
                "text":      m['text'],
                "sent_at":   m['sent_at'].isoformat(),
            }
            for m in messages
        ],
        "members": [
            {"id": mb.user.id, "name": mb.user.name, "email": mb.user.email}
            for mb in members
        ],
        "is_host": chat.meeting.host == request.user,
        "meeting_url": chat.meeting.meeting_url,
        "meeting_id":  chat.meeting.meeting_id,
        "passcode":    chat.meeting.passcode,
    })


@login_required
@require_POST
def chat_send(request, chat_id):
    chat = get_object_or_404(MeetingChat, id=chat_id)
    member = MeetingChatMember.objects.filter(chat=chat, user=request.user, is_removed=False).first()
    if not member:
        return JsonResponse({"error": "Not a member"}, status=403)

    body = json.loads(request.body)
    text = body.get("text", "").strip()
    if not text:
        return JsonResponse({"error": "Empty message"}, status=400)

    msg = MeetingChatMessage.objects.create(chat=chat, sender=request.user, text=text)
    return JsonResponse({"id": msg.id, "sent_at": msg.sent_at.isoformat()})


@login_required
@require_POST
def chat_delete_for_me(request, chat_id):
    """User hides the chat from their view — others still see it."""
    membership = get_object_or_404(MeetingChatMember, chat_id=chat_id, user=request.user)
    membership.deleted_for_self = True
    membership.save(update_fields=['deleted_for_self'])
    return JsonResponse({"status": "hidden"})


@login_required
@require_POST
def chat_add_member(request, chat_id):
    chat = get_object_or_404(MeetingChat, id=chat_id)
    if chat.meeting.host != request.user:
        return JsonResponse({"error": "Only host can add members"}, status=403)

    body  = json.loads(request.body)
    email = body.get("email", "").strip().lower()
    from django.contrib.auth import get_user_model
    User = get_user_model()
    try:
        user = User.objects.get(email=email)
    except User.DoesNotExist:
        return JsonResponse({"error": "User not found"}, status=404)

    mb, created = MeetingChatMember.objects.get_or_create(chat=chat, user=user)
    if not created:
        mb.is_removed = False
        mb.deleted_for_self = False
        mb.save()
    # Also save as invitee
    MeetingInvitee.objects.get_or_create(meeting=chat.meeting, email=email)
    return JsonResponse({"status": "added", "name": user.name})


@login_required
@require_POST
def chat_remove_member(request, chat_id, user_id):
    chat = get_object_or_404(MeetingChat, id=chat_id)
    if chat.meeting.host != request.user:
        return JsonResponse({"error": "Only host can remove members"}, status=403)

    mb = get_object_or_404(MeetingChatMember, chat=chat, user_id=user_id)
    mb.is_removed = True
    mb.save(update_fields=['is_removed'])
    return JsonResponse({"status": "removed"})


@login_required
def all_meetings_page(request):
    """Dedicated page that shows all scheduled meetings for the current user."""
    return render(request, "meetings/all_meetings.html")


@login_required
def scheduled_meetings_for_date(request):
    from datetime import date, datetime, timedelta

    date_str = request.GET.get("date")
    try:
        target_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        return JsonResponse({"error": "Invalid date"}, status=400)

    # Hosted by user OR invited
    hosted = Meeting.objects.filter(host=request.user, is_scheduled=True)
    invited = Meeting.objects.filter(
        is_scheduled=True,
        invitees__email=request.user.email,
    ).exclude(host=request.user)

    all_meetings = (hosted | invited).distinct()

    result = []
    for m in all_meetings:
        if not m.scheduled_start:
            continue

        orig_date = m.scheduled_start.date()
        repeat    = m.repeat or 'none'
        rep_end   = m.repeat_end_date  # date or None

        # Check if this meeting occurs on target_date
        occurs = False

        if repeat == 'none':
            occurs = (orig_date == target_date)

        elif repeat == 'daily':
            occurs = (
                target_date >= orig_date and
                (rep_end is None or target_date <= rep_end)
            )

        elif repeat == 'weekday':
            occurs = (
                target_date >= orig_date and
                target_date.weekday() < 5 and          # Mon–Fri
                (rep_end is None or target_date <= rep_end)
            )

        elif repeat == 'weekly':
            delta = (target_date - orig_date).days
            occurs = (
                target_date >= orig_date and
                delta % 7 == 0 and
                (rep_end is None or target_date <= rep_end)
            )

        elif repeat == 'monthly':
            occurs = (
                target_date >= orig_date and
                target_date.day == orig_date.day and
                (rep_end is None or target_date <= rep_end)
            )

        elif repeat == 'yearly':
            occurs = (
                target_date >= orig_date and
                target_date.day   == orig_date.day and
                target_date.month == orig_date.month and
                (rep_end is None or target_date <= rep_end)
            )

        if not occurs:
            continue

        # Build adjusted start/end for the target date
        if m.is_all_day or repeat == 'none':
            adj_start = m.scheduled_start
            adj_end   = m.scheduled_end
        else:
            # Shift time to target_date, keeping original clock time
            from django.utils import timezone as tz
            orig_start = m.scheduled_start
            orig_end   = m.scheduled_end

            def shift_to(dt, new_date):
                if dt is None:
                    return None
                delta_days = (new_date - orig_date).days
                return dt + timedelta(days=delta_days)

            adj_start = shift_to(orig_start, target_date)
            adj_end   = shift_to(orig_end,   target_date)

        result.append({
            "id":          m.id,
            "title":       m.title,
            "meeting_id":  m.meeting_id,
            "passcode":    m.passcode,
            "room_name":   m.room_name,
            "meeting_url": m.meeting_url,
            "start":       adj_start.isoformat() if adj_start else None,
            "end":         adj_end.isoformat()   if adj_end   else None,
            "original_start": m.scheduled_start.isoformat() if m.scheduled_start else None,
            "original_end":   m.scheduled_end.isoformat()   if m.scheduled_end   else None,
            "is_all_day":    m.is_all_day,
            "repeat":        m.repeat,
            "repeat_end_date": m.repeat_end_date.isoformat() if m.repeat_end_date else None,
            "is_host":       m.host == request.user,
            "is_active":     m.is_active,
            "invitees":      list(m.invitees.values_list('email', flat=True)),
        })

    result.sort(key=lambda x: x['start'] or '')
    return JsonResponse({"meetings": result, "date": target_date.isoformat()})


@login_required
def all_scheduled_meetings(request):
    """
    GET /meeting/all-scheduled/
    Returns ALL scheduled meetings for the current user (hosted + invited), grouped or flat.
    """
    # Meetings hosted by user
    hosted = Meeting.objects.filter(
        host=request.user,
        is_scheduled=True,
    )

    # Meetings where user is an invitee
    invited = Meeting.objects.filter(
        is_scheduled=True,
        invitees__email=request.user.email,
    ).exclude(host=request.user)

    all_meetings = (hosted | invited).distinct().order_by('scheduled_start')

    result = []
    for m in all_meetings:
        result.append({
            "id":          m.id,
            "title":       m.title,
            "meeting_id":  m.meeting_id,
            "passcode":    m.passcode,
            "room_name":   m.room_name,
            "meeting_url": m.meeting_url,
            "start":       m.scheduled_start.isoformat() if m.scheduled_start else None,
            "end":         m.scheduled_end.isoformat()   if m.scheduled_end   else None,
            "is_all_day":  m.is_all_day,
            "repeat":      m.repeat,
            "repeat_end_date": m.repeat_end_date.isoformat() if m.repeat_end_date else None,
            "is_host":     m.host == request.user,
            "is_active":   m.is_active,
            "invitees":    list(m.invitees.values_list('email', flat=True)),
        })

    return JsonResponse({"meetings": result})


@login_required
@require_POST
def edit_meeting(request, meeting_id):
    """
    POST /meeting/edit/<id>/
    Host can update title, start, end, repeat, invitees.
    """
    meeting = get_object_or_404(Meeting, id=meeting_id, host=request.user)

    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    title      = body.get("title", meeting.title).strip() or meeting.title
    start_str  = body.get("start")
    end_str    = body.get("end")
    is_all_day = body.get("all_day", meeting.is_all_day)
    repeat     = body.get("repeat", meeting.repeat)
    repeat_end = body.get("repeat_end")
    invitee_emails = [e.strip().lower() for e in body.get("invitees", []) if e.strip()]

    meeting.title      = title
    meeting.is_all_day = is_all_day
    meeting.repeat     = repeat

    if start_str:
        dt = parse_datetime(start_str)
        meeting.scheduled_start = dt if dt.tzinfo else timezone.make_aware(dt)
    if end_str:
        dt = parse_datetime(end_str)
        meeting.scheduled_end = dt if dt.tzinfo else timezone.make_aware(dt)
    if repeat == 'none':
        meeting.repeat_end_date = None
    elif repeat_end:
        from datetime import date
        meeting.repeat_end_date = date.fromisoformat(repeat_end)
    else:
        meeting.repeat_end_date = None
    

    meeting.save()

    # Update invitees — replace list
    meeting.invitees.all().delete()
    for email in invitee_emails:
        MeetingInvitee.objects.get_or_create(meeting=meeting, email=email)

    # Sync chat members
    try:
        chat = meeting.chat
        for email in invitee_emails:
            from django.contrib.auth import get_user_model
            User = get_user_model()
            try:
                u = User.objects.get(email=email)
                mb, _ = MeetingChatMember.objects.get_or_create(chat=chat, user=u)
                mb.is_removed = False
                mb.save()
            except User.DoesNotExist:
                pass
    except Exception:
        pass

    return JsonResponse({"status": "updated"})


@csrf_exempt  
@login_required
@require_POST
def deactivate_meeting(request, room_name):
    meeting = Meeting.objects.filter(room_name=room_name, host=request.user).first()
    if meeting and meeting.is_active:
        meeting.is_active = False
        meeting.save(update_fields=['is_active'])
    return JsonResponse({"status": "ok"})




@login_required
@require_POST
def delete_meeting(request, meeting_id):
    """
    POST /meeting/delete/<id>/
    Host can delete their scheduled meeting.
    """
    meeting = get_object_or_404(Meeting, id=meeting_id, host=request.user)
    meeting.delete()
    return JsonResponse({"status": "deleted"})


# ── CHAT POLL (real-time new messages since a timestamp) ─────────────────────

@login_required
def chat_poll(request, chat_id):
    """GET /meeting/chat/<id>/poll/?since=<iso>  — returns only NEW messages."""
    chat   = get_object_or_404(MeetingChat, id=chat_id)
    member = MeetingChatMember.objects.filter(
        chat=chat, user=request.user, is_removed=False
    ).first()
    if not member:
        return JsonResponse({"error": "Not a member"}, status=403)

    since = request.GET.get("since")
    qs    = chat.messages.exclude(deleted_by=request.user)
    if since:
        try:
            from django.utils.dateparse import parse_datetime as _pd
            dt = _pd(since)
            if dt:
                qs = qs.filter(sent_at__gt=dt)
        except Exception:
            pass

    msgs = qs.order_by("sent_at").values(
        "id", "sender__name", "sender_id", "text", "sent_at"
    )
    return JsonResponse({
        "messages": [
            {
                "id":      m["id"],
                "sender":  m["sender__name"],
                "is_me":   m["sender_id"] == request.user.id,
                "text":    m["text"],
                "sent_at": m["sent_at"].isoformat(),
            }
            for m in msgs
        ]
    })


# ── CHAT PAGE ─────────────────────────────────────────────────────────────────

@login_required
def chat_page(request):
    return render(request, "meetings/chat.html")


# ── DIRECT MESSAGE VIEWS ──────────────────────────────────────────────────────

@login_required
def dm_list(request):
    """GET /meeting/dm/  — list all DM conversations for the current user."""
    participations = DirectChatParticipant.objects.filter(
        user=request.user
    ).select_related('chat')

    result = []
    for p in participations:
        dm    = p.chat
        other = dm.participations.exclude(user=request.user).select_related('user').first()
        if not other:
            continue
        last = dm.messages.filter(is_deleted=False).order_by('-sent_at').first()
        result.append({
            "dm_id":      dm.id,
            "other_user": {
                "id":    other.user.id,
                "name":  other.user.name,
                "email": other.user.email,
            },
            "last_message": last.text      if last else "",
            "last_at":      last.sent_at.isoformat() if last else "",
        })

    # Sort most recent first
    result.sort(key=lambda x: x["last_at"] or "", reverse=True)
    return JsonResponse({"dms": result})


@login_required
@require_POST
def dm_get_or_create(request):
    """POST /meeting/dm/get-or-create/  {user_id}  — get or create a DM thread."""
    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    user_id = body.get("user_id")
    from django.contrib.auth import get_user_model
    User = get_user_model()
    try:
        other_user = User.objects.get(id=user_id)
    except User.DoesNotExist:
        return JsonResponse({"error": "User not found"}, status=404)

    if other_user == request.user:
        return JsonResponse({"error": "Cannot DM yourself"}, status=400)

    # Find existing DM between these two users
    my_dm_ids    = DirectChatParticipant.objects.filter(user=request.user).values_list('chat_id', flat=True)
    other_dm_ids = DirectChatParticipant.objects.filter(user=other_user).values_list('chat_id', flat=True)
    shared       = set(my_dm_ids) & set(other_dm_ids)

    if shared:
        dm_id = list(shared)[0]
        return JsonResponse({"dm_id": dm_id, "created": False})

    dm = DirectChat.objects.create()
    DirectChatParticipant.objects.create(chat=dm, user=request.user)
    DirectChatParticipant.objects.create(chat=dm, user=other_user)
    return JsonResponse({"dm_id": dm.id, "created": True})


@login_required
def dm_messages(request, dm_id):
    """GET /meeting/dm/<id>/  — messages (supports ?since= for polling)."""
    dm = get_object_or_404(DirectChat, id=dm_id)
    if not dm.participations.filter(user=request.user).exists():
        return JsonResponse({"error": "Not a participant"}, status=403)

    since = request.GET.get("since")
    qs    = dm.messages.filter(is_deleted=False)
    if since:
        try:
            from django.utils.dateparse import parse_datetime as _pd
            dt = _pd(since)
            if dt:
                qs = qs.filter(sent_at__gt=dt)
        except Exception:
            pass

    msgs  = qs.order_by("sent_at").select_related("sender")
    other = dm.participations.exclude(user=request.user).select_related("user").first()

    return JsonResponse({
        "messages": [
            {
                "id":      m.id,
                "sender":  m.sender.name,
                "is_me":   m.sender_id == request.user.id,
                "text":    m.text,
                "sent_at": m.sent_at.isoformat(),
            }
            for m in msgs
        ],
        "other_user": {
            "id":   other.user.id,
            "name": other.user.name,
        } if other else None,
    })


@login_required
@require_POST
def dm_send(request, dm_id):
    """POST /meeting/dm/<id>/send/  {text}  — send a DM."""
    dm = get_object_or_404(DirectChat, id=dm_id)
    if not dm.participations.filter(user=request.user).exists():
        return JsonResponse({"error": "Not a participant"}, status=403)

    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    text = body.get("text", "").strip()
    if not text:
        return JsonResponse({"error": "Empty message"}, status=400)

    msg = DirectChatMessage.objects.create(chat=dm, sender=request.user, text=text)
    return JsonResponse({"id": msg.id, "sent_at": msg.sent_at.isoformat()})


# ── USER SEARCH (for starting DMs) ───────────────────────────────────────────

@login_required
def search_users(request):
    """GET /meeting/users/search/?q=<query>"""
    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"users": []})

    from django.contrib.auth import get_user_model
    from django.db.models import Q
    User = get_user_model()

    users = User.objects.filter(
        Q(name__icontains=q) | Q(email__icontains=q)
    ).exclude(pk=request.user.pk)[:10]

    return JsonResponse({
        "users": [
            {"id": u.id, "name": u.name, "email": u.email}
            for u in users
        ]
    })
    
    
    
@login_required   # ← add this missing decorator
def settings_page(request):
    user = request.user

    if request.method == "POST":

        # ==========================
        # 🔐 PASSWORD FORM
        # ==========================
        if "password_form" in request.POST:
            new_password     = request.POST.get("new_password", "").strip()
            confirm_password = request.POST.get("confirm_password", "").strip()
            current_password = request.POST.get("current_password", "").strip()

            if not new_password:
                messages.error(request, "New password cannot be empty.")
                return redirect("meetings:settings_page")

            if new_password != confirm_password:
                messages.error(request, "Passwords do not match.")
                return redirect("meetings:settings_page")

            if user.has_usable_password():
                if not current_password:
                    messages.error(request, "Current password is required.")
                    return redirect("meetings:settings_page")
                if not check_password(current_password, user.password):
                    messages.error(request, "Current password is incorrect.")
                    return redirect("meetings:settings_page")

            user.set_password(new_password)
            user.save()
            update_session_auth_hash(request, user)
            messages.success(request, "Password updated successfully.")
            return redirect("meetings:settings_page")

        # ==========================
        # 🖼 REMOVE PHOTO
        # ==========================
        if "remove_photo" in request.POST:
            if user.user_profile_picture:
                user.user_profile_picture.delete(save=False)
                user.user_profile_picture = None
                user.save()
            return redirect("meetings:settings_page")  # ← fixed typo "mettings"

        # ==========================
        # 👤 PROFILE UPDATE
        # ==========================
        if request.FILES.get("profile_picture"):
            profile_file = request.FILES["profile_picture"]
            if user.user_profile_picture:
                user.user_profile_picture.delete(save=False)
            user.user_profile_picture = profile_file

        user.name  = request.POST.get("name", user.name).strip()
        user.email = request.POST.get("email", user.email).strip()
        user.save()
        messages.success(request, "Profile updated successfully.")
        return redirect("meetings:settings_page")

    # ==========================
    # GET — build profile URL safely
    # ==========================
    profile_image_url = None
    if user.user_profile_picture:
        try:
            profile_image_url = user.user_profile_picture.url  # ← .url not the field
        except Exception:
            profile_image_url = None

    return render(request, "meetings/settings.html", {
        "profile_image_url": profile_image_url,
    })
    
    
    
@login_required
@require_POST
def save_transcript(request):
    file      = request.FILES.get('transcript')
    room_name = request.POST.get('room_name')
    duration  = int(request.POST.get('duration_seconds', 0))
    if not file or not room_name:
        return JsonResponse({'error': 'Missing data'}, status=400)
    try:
        meeting = Meeting.objects.get(room_name=room_name)
        transcript = MeetingTranscript.objects.create(
            meeting=meeting, file=file, duration_seconds=duration
        )
        return JsonResponse({'url': transcript.file.url, 'id': transcript.id})
    except Meeting.DoesNotExist:
        return JsonResponse({'error': 'Meeting not found'}, status=404)
    except Exception as e:
        return JsonResponse({'error': str(e)}, status=500)


@login_required
@require_POST
def delete_transcript(request, transcript_id):
    try:
        t = get_object_or_404(MeetingTranscript, id=transcript_id, meeting__host=request.user)
        if t.file:
            try:
                t.file.delete(save=False)
            except Exception as e:
                print(f"Transcript file delete warning: {e}")
        t.delete()
        return JsonResponse({'status': 'deleted'})
    except Exception as e:
        return JsonResponse({'status': 'error', 'error': str(e)}, status=500)


@login_required
def transcripts_hub(request):
    transcripts = MeetingTranscript.objects.filter(
        meeting__host=request.user
    ).select_related('meeting').order_by('-recorded_at')
    return render(request, 'meetings/transcripts_hub.html', {'transcripts': transcripts})