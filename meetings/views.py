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
from .models import Meeting, MeetingInvitee, MeetingChat, MeetingChatMember, MeetingChatMessage
from django.utils.dateparse import parse_datetime
from django.utils import timezone


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

    token = AccessToken(
        api_key=settings.LIVEKIT_API_KEY,
        api_secret=settings.LIVEKIT_API_SECRET,
    )
    token.with_identity(str(request.user.id))
    token.with_name(request.user.name)
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
        "user_name": request.user.name,
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

    return render(request, "meetings/room.html", {
        "meeting": meeting,
        "room_name": room_name,
        "is_host": meeting.host == request.user,
        "meeting_url": meeting.meeting_url,
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

    async def _end():
        async with LiveKitAPI(
            url=settings.LIVEKIT_URL,
            api_key=settings.LIVEKIT_API_KEY,
            api_secret=settings.LIVEKIT_API_SECRET,
        ) as lk:
            # Set metadata first — fires RoomMetadataChanged on all non-host clients
            try:
                from livekit.api import UpdateRoomMetadataRequest
                await lk.room.update_room_metadata(
                    UpdateRoomMetadataRequest(room=room_name, metadata="ended")
                )
            except Exception as e:
                print(f"update_room_metadata failed: {e}")

            # Small wait for metadata to propagate to clients, then delete
            import asyncio as _a
            await _a.sleep(0.5)   # 0.5s only — just enough for metadata delivery

            try:
                await lk.room.delete_room(DeleteRoomRequest(room=room_name))
            except Exception as e:
                print(f"delete_room failed: {e}")

    asyncio.run(_end())

    if meeting:
        meeting.is_active = False
        meeting.save()

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

    if not room_name or not file:
        return JsonResponse({"error": "Missing data"}, status=400)

    try:
        meeting = Meeting.objects.get(room_name=room_name)
    except Meeting.DoesNotExist:
        return JsonResponse({"error": "Meeting not found"}, status=404)

    rec = MeetingRecording.objects.create(meeting=meeting, file=file)
    return JsonResponse({"url": rec.file.url})



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
            "is_all_day":  m.is_all_day,
            "repeat":      m.repeat,
            "repeat_end_date": m.repeat_end_date.isoformat() if m.repeat_end_date else None,
            "is_host":     m.host == request.user,
            "is_active":   m.is_active,
            "invitees":    list(m.invitees.values_list('email', flat=True)),
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
