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
from .models import Meeting, MeetingRecording, WaitingRoomKnock
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

    display_name = request.GET.get("display_name", "").strip() or request.user.name

    # Resolve plan limits
    max_participants, max_duration_minutes = _get_plan_limits(request.user)
    features = _get_plan_features(request.user)
    sub = _get_or_create_subscription(request.user)
    plan_name = sub.effective_plan.name if sub else "free"

    # ── Capacity check (skip for host) ───────────────────────────────────────
    if meeting.host != request.user and meeting.max_participants and meeting.max_participants > 0:
        current_count = _get_livekit_participant_count(room_name)
        if current_count >= meeting.max_participants:
            # Register user in capacity queue (first-come-first-served)
            _enqueue_capacity_waiter(meeting, request.user, display_name)
            return JsonResponse(
                {
                    "code":  "MEETING_FULL",
                    "limit": meeting.max_participants,
                    "queue_position": _get_queue_position(meeting, request.user),
                },
                status=403,
            )

    # If user was in capacity queue but space opened, remove them
    _dequeue_capacity_waiter(meeting, request.user)

    token = AccessToken(
        api_key=settings.LIVEKIT_API_KEY,
        api_secret=settings.LIVEKIT_API_SECRET,
    )
    token.with_identity(str(request.user.id))
    token.with_name(display_name)
    token.with_grants(VideoGrants(
        room_join=True,
        room=room_name,
        can_publish=True,
        can_subscribe=True,
        can_publish_data=True,
    ))

    return JsonResponse({
        "token":                 token.to_jwt(),
        "livekit_url":           settings.LIVEKIT_URL,
        "room_name":             room_name,
        "user_name":             display_name,
        "is_host":               meeting.host == request.user,
        "activated_at":          meeting.activated_at.isoformat() if meeting.activated_at else "",
        "max_duration_minutes":  meeting.max_duration_minutes,
        "max_participants":      meeting.max_participants,
        "can_record":            features["can_record"],
        "plan_name":             plan_name,
        "only_host_audio":       meeting.only_host_audio,
        "only_host_video":       meeting.only_host_video,
        "only_host_chat":        meeting.only_host_chat,
        "only_host_screenshare": meeting.only_host_screenshare,
    })
    
    
    
# ─────────────────────────────────────────────────────────────────────────────
# NEW: Capacity queue helpers  (add near the bottom of views.py)
# ─────────────────────────────────────────────────────────────────────────────

def _get_livekit_participant_count(room_name):
    """
    Returns the current number of participants in the LiveKit room.
    Returns 0 on any error so we fail open (don't block entry on API failure).
    """
    import asyncio as _asyncio

    async def _count():
        try:
            async with LiveKitAPI(
                url=settings.LIVEKIT_URL,
                api_key=settings.LIVEKIT_API_KEY,
                api_secret=settings.LIVEKIT_API_SECRET,
            ) as lk:
                res = await lk.room.list_participants(
                    ListParticipantsRequest(room=room_name)
                )
                return len(res.participants)
        except Exception:
            return 0

    try:
        loop = _asyncio.new_event_loop()
        count = loop.run_until_complete(_count())
        loop.close()
        return count
    except Exception:
        return 0


def _enqueue_capacity_waiter(meeting, user, display_name=""):
    """Add user to capacity queue if not already there."""
    from .models import MeetingCapacityQueue
    MeetingCapacityQueue.objects.get_or_create(
        meeting=meeting,
        user=user,
        defaults={"display_name": display_name or user.name},
    )


def _dequeue_capacity_waiter(meeting, user):
    """Remove user from capacity queue (they got in)."""
    from .models import MeetingCapacityQueue
    MeetingCapacityQueue.objects.filter(meeting=meeting, user=user).delete()


def _get_queue_position(meeting, user):
    """1-based queue position for the user, or None if not in queue."""
    from .models import MeetingCapacityQueue
    try:
        entry = MeetingCapacityQueue.objects.get(meeting=meeting, user=user)
        position = MeetingCapacityQueue.objects.filter(
            meeting=meeting,
            created_at__lte=entry.created_at,
        ).count()
        return position
    except Exception:
        return None


# ─────────────────────────────────────────────────────────────────────────────
# NEW VIEW: capacity_status  — polled by waiting users
# GET /meeting/capacity-status/<room_name>/
# Returns: { full, queue_position, space_available }
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def capacity_status(request, room_name):
    from .models import MeetingCapacityQueue
    meeting = get_object_or_404(Meeting, room_name=room_name)

    current_count = _get_livekit_participant_count(room_name)
    limit = meeting.max_participants or 0
    is_full = limit > 0 and current_count >= limit

    # Check if this user is queued
    queue_entry = MeetingCapacityQueue.objects.filter(
        meeting=meeting, user=request.user
    ).first()

    queue_position = None
    if queue_entry:
        queue_position = MeetingCapacityQueue.objects.filter(
            meeting=meeting,
            created_at__lte=queue_entry.created_at,
        ).count()

    # Is this the next person in line and there's now space?
    space_available = False
    if queue_entry and not is_full:
        # Is this user at the front of the queue?
        first_in_queue = MeetingCapacityQueue.objects.filter(
            meeting=meeting
        ).order_by("created_at").first()
        if first_in_queue and first_in_queue.user_id == request.user.id:
            space_available = True

    return JsonResponse({
        "full":            is_full,
        "current_count":   current_count,
        "limit":           limit,
        "queue_position":  queue_position,
        "space_available": space_available,
        "meeting_active":  meeting.is_active,
    })


# ─────────────────────────────────────────────────────────────────────────────
# NEW VIEW: capacity_queue_list  — host sees who is queued
# GET /meeting/capacity-queue/<room_name>/
# ─────────────────────────────────────────────────────────────────────────────

@login_required
def capacity_queue_list(request, room_name):
    from .models import MeetingCapacityQueue
    meeting = get_object_or_404(Meeting, room_name=room_name)
    if meeting.host != request.user:
        return JsonResponse({"error": "Host only"}, status=403)

    queue = MeetingCapacityQueue.objects.filter(
        meeting=meeting
    ).select_related("user").order_by("created_at")

    return JsonResponse({
        "queue": [
            {
                "user_id":      q.user.id,
                "display_name": q.display_name or q.user.name,
                "position":     idx + 1,
                "joined_at":    q.created_at.isoformat(),
            }
            for idx, q in enumerate(queue)
        ]
    })


######################################################
# ROOM VIEW  (updated)
######################################################

@login_required
def room(request, room_name):
    meeting = Meeting.objects.filter(room_name=room_name).first()

    if not meeting:
        messages.error(request, "Meeting not found.")
        return redirect('meetings:dashboard')

    if not meeting.meeting_url:
        meeting.meeting_url = _build_meeting_url(request, room_name)
        meeting.save(update_fields=["meeting_url"])

    is_host = (meeting.host == request.user)

    # ── Host activates meeting on entry ──────────────────────────────────────
    if is_host:
        update_fields = ["is_active"]
        meeting.is_active = True
        # Only set activated_at ONCE, never overwrite it
        if not meeting.activated_at:
            meeting.activated_at = timezone.now()
            update_fields.append("activated_at")
        meeting.save(update_fields=update_fields)

        # Resolve plan limits
        max_participants, max_duration_minutes = _get_plan_limits(request.user)
        features = _get_plan_features(request.user)

        # Snapshot limits onto meeting if not already set from creation
        # (guards against plan changes after meeting was created)
        meeting.max_participants = max_participants
        meeting.max_duration_minutes = max_duration_minutes
        meeting.save(update_fields=["max_participants", "max_duration_minutes"])

        chat = _ensure_meeting_chat(meeting, request.user)

        sub = _get_or_create_subscription(request.user)
        plan_name = sub.effective_plan.name if sub else "free"

        return render(request, "meetings/room.html", {
            "meeting":               meeting,
            "room_name":             room_name,
            "is_host":               True,
            "meeting_url":           meeting.meeting_url,
            "chat_id":               chat.id,
            "max_participants":      max_participants,
            "max_duration_minutes":  max_duration_minutes,
            "can_record":            features["can_record"],
            "plan_name":             plan_name,
            "only_host_audio":       meeting.only_host_audio,
            "only_host_video":       meeting.only_host_video,
            "only_host_chat":        meeting.only_host_chat,
            "only_host_screenshare": meeting.only_host_screenshare,
            # Pass ISO string so JS timer syncs to server clock, not local clock
            "activated_at_iso":      meeting.activated_at.isoformat() if meeting.activated_at else "",
        })

    # ── Non-host: meeting not started yet → waiting room ─────────────────────
    if not meeting.is_active:
        display_name = (
            request.GET.get("display_name", "").strip()
            or request.session.get("join_display_name", "")
            or request.user.name
        )
        return render(request, "meetings/waiting_room.html", {
            "meeting":           meeting,
            "room_name":         room_name,
            "display_name":      display_name,
            "only_host_audio":   meeting.only_host_audio,   # ADD
            "only_host_video":   meeting.only_host_video,   # ADD
        })

    # ── Non-host: meeting is active but requires admission ───────────────────
    if meeting.require_admission:
        # Check if this user already has an admission decision
        knock = WaitingRoomKnock.objects.filter(
            meeting=meeting, user=request.user
        ).first()

        if knock is None or knock.status == 'waiting':
            # Not yet admitted — send to waiting room
            display_name = (
                request.GET.get("display_name", "").strip()
                or request.session.get("join_display_name", "")
                or request.user.name
            )
            # Auto-create the knock record so host can see them
            if knock is None:
                WaitingRoomKnock.objects.create(
                    meeting=meeting,
                    user=request.user,
                    display_name=display_name,
                    status='waiting',
                )
            return render(request, "meetings/waiting_room.html", {
                "meeting":           meeting,
                "room_name":         room_name,
                "display_name":      display_name,
                "only_host_audio":   meeting.only_host_audio,   # ADD
                "only_host_video":   meeting.only_host_video,   # ADD
            })

        elif knock.status == 'denied':
            messages.error(request, "The host declined your request to join.")
            return redirect('meetings:dashboard')

        # admitted → fall through to normal room render
        
    if request.GET.get('queued') == '1':
        display_name = (
            request.GET.get("display_name", "").strip()
            or request.session.get("join_display_name", "")
            or request.user.name
        )
        return render(request, "meetings/waiting_room.html", {
            "meeting":           meeting,
            "room_name":         room_name,
            "display_name":      display_name,
            "start_in_queue":    True,
            "only_host_audio":   meeting.only_host_audio,   # ADD
            "only_host_video":   meeting.only_host_video,   # ADD
        })

    # ── Non-host: enter meeting ───────────────────────────────────────────────
    chat = _ensure_meeting_chat(meeting, request.user)
    return render(request, "meetings/room.html", {
        "meeting":               meeting,
        "room_name":             room_name,
        "is_host":               False,
        "meeting_url":           meeting.meeting_url,
        "chat_id":               chat.id,
        "only_host_audio":       meeting.only_host_audio,
        "only_host_video":       meeting.only_host_video,
        "only_host_chat":        meeting.only_host_chat,
        "only_host_screenshare": meeting.only_host_screenshare,
        "activated_at_iso":      meeting.activated_at.isoformat() if meeting.activated_at else "",
        "max_participants":      meeting.max_participants or 0,
        "max_duration_minutes":  meeting.max_duration_minutes or 0,
        "can_record":            False,
        "plan_name":             "",
    })


######################################################
# WAITING ROOM VIEWS  (all NEW)
######################################################

@login_required
def waiting_room(request, room_name):
    """Explicit waiting room view (also rendered by room() above)."""
    meeting = get_object_or_404(Meeting, room_name=room_name)
    display_name = (
        request.GET.get("display_name", "").strip()
        or request.user.name
    )
    return render(request, "meetings/waiting_room.html", {
        "meeting":           meeting,
        "room_name":         room_name,
        "display_name":      display_name,
        "only_host_audio":   meeting.only_host_audio,   # ADD
        "only_host_video":   meeting.only_host_video,   # ADD
    })


@login_required
def waiting_status(request, room_name):
    """
    GET /meeting/waiting-status/<room>/
    Poll endpoint for the waiting room page.
    Returns { meeting_active, require_admission }
    """
    meeting = get_object_or_404(Meeting, room_name=room_name)
    return JsonResponse({
        "meeting_active":    meeting.is_active,
        "require_admission": meeting.require_admission,
    })


@login_required
@require_POST
def knock(request, room_name):
    """
    POST /meeting/knock/<room>/
    Register (or re-register) the user as knocking.
    Body: { display_name }
    """
    meeting = get_object_or_404(Meeting, room_name=room_name)
    try:
        body = json.loads(request.body)
    except Exception:
        body = {}

    display_name = body.get("display_name", "").strip() or request.user.name

    knock_obj, created = WaitingRoomKnock.objects.get_or_create(
        meeting=meeting,
        user=request.user,
        defaults={"display_name": display_name, "status": "waiting"},
    )
    if not created and knock_obj.status == "denied":
        # Allow re-knock if previously denied (host can change mind)
        knock_obj.status = "waiting"
        knock_obj.display_name = display_name
        knock_obj.save(update_fields=["status", "display_name", "updated_at"])

    return JsonResponse({"status": knock_obj.status})


@login_required
@require_POST
def knock_cancel(request, room_name):
    """
    POST /meeting/knock-cancel/<room>/
    User left the waiting room — remove their knock.
    """
    meeting = get_object_or_404(Meeting, room_name=room_name)
    WaitingRoomKnock.objects.filter(meeting=meeting, user=request.user).delete()
    return JsonResponse({"status": "cancelled"})


@login_required
def admission_status(request, room_name):
    """
    GET /meeting/admission-status/<room>/
    The waiting user polls this to find out if they were admitted or denied.
    Returns { status: 'waiting' | 'admitted' | 'denied' }
    """
    meeting = get_object_or_404(Meeting, room_name=room_name)
    knock_obj = WaitingRoomKnock.objects.filter(
        meeting=meeting, user=request.user
    ).first()

    if not knock_obj:
        # If meeting doesn't require admission OR knock was never created → admitted
        status = "admitted" if not meeting.require_admission else "waiting"
    else:
        status = knock_obj.status

    return JsonResponse({"status": status})


@login_required
def knock_list(request, room_name):
    """
    GET /meeting/knock-list/<room>/
    HOST ONLY — returns list of users currently waiting.
    """
    meeting = get_object_or_404(Meeting, room_name=room_name)
    if meeting.host != request.user:
        return JsonResponse({"error": "Host only"}, status=403)

    knocks = WaitingRoomKnock.objects.filter(
        meeting=meeting, status="waiting"
    ).select_related("user").order_by("created_at")

    return JsonResponse({
        "knocks": [
            {
                "knock_id":     k.id,
                "user_id":      k.user.id,
                "display_name": k.display_name or k.user.name,
                "avatar":       (k.display_name or k.user.name or "?")[0].upper(),
            }
            for k in knocks
        ]
    })


@login_required
@require_POST
def admit_user(request, room_name, user_id):
    """
    POST /meeting/admit/<room>/<user_id>/
    Host admits a specific user.
    """
    meeting = get_object_or_404(Meeting, room_name=room_name)
    if meeting.host != request.user:
        return JsonResponse({"error": "Host only"}, status=403)

    updated = WaitingRoomKnock.objects.filter(
        meeting=meeting, user_id=user_id
    ).update(status="admitted")

    return JsonResponse({"admitted": bool(updated)})


@login_required
@require_POST
def deny_user(request, room_name, user_id):
    """
    POST /meeting/deny/<room>/<user_id>/
    Host denies a specific user.
    """
    meeting = get_object_or_404(Meeting, room_name=room_name)
    if meeting.host != request.user:
        return JsonResponse({"error": "Host only"}, status=403)

    updated = WaitingRoomKnock.objects.filter(
        meeting=meeting, user_id=user_id
    ).update(status="denied")

    return JsonResponse({"denied": bool(updated)})


@login_required
@require_POST
def admit_all_users(request, room_name):
    """POST /meeting/admit-all/<room>/  — Host admits everyone waiting."""
    meeting = get_object_or_404(Meeting, room_name=room_name)
    if meeting.host != request.user:
        return JsonResponse({"error": "Host only"}, status=403)

    count = WaitingRoomKnock.objects.filter(
        meeting=meeting, status="waiting"
    ).update(status="admitted")

    return JsonResponse({"admitted_count": count})


@login_required
@require_POST
def deny_all_users(request, room_name):
    """POST /meeting/deny-all/<room>/  — Host denies everyone waiting."""
    meeting = get_object_or_404(Meeting, room_name=room_name)
    if meeting.host != request.user:
        return JsonResponse({"error": "Host only"}, status=403)

    count = WaitingRoomKnock.objects.filter(
        meeting=meeting, status="waiting"
    ).update(status="denied")

    return JsonResponse({"denied_count": count})


######################################################
# CREATE MEETING  (updated — passes require_admission)
######################################################

@login_required
def create_meeting(request):
    title = request.GET.get("title") or request.POST.get("title", "Quick Meeting")
    room_name = secrets.token_hex(32)

    # Support require_admission from JSON body (dashboard POST)
    require_admission = False
    if request.method == "POST" and request.content_type == "application/json":
        try:
            body = json.loads(request.body)
            title = body.get("title", title)
            require_admission = bool(body.get("require_admission", False))
        except Exception:
            pass
    elif request.method == "POST":
        require_admission = request.POST.get("require_admission") == "1"

    meeting = Meeting.objects.create(
        host=request.user,
        room_name=room_name,
        title=title,
        require_admission=require_admission,
    )
    meeting.meeting_url = _build_meeting_url(request, room_name)
    meeting.save(update_fields=["meeting_url"])

    _ensure_meeting_chat(meeting, request.user)

    redirect_url = f"/room/{room_name}/"

    if request.method == "POST":
        return JsonResponse({"redirect": redirect_url, "room_name": room_name})

    return redirect('meetings:meeting_room', room_name=room_name)


######################################################
# JOIN BY MEETING ID + PASSCODE
######################################################

@login_required
@require_POST
def join_by_id(request):
    try:
        body = json.loads(request.body)
    except Exception:
        body = request.POST

    meeting_id = str(body.get("meeting_id", "")).strip().replace(" ", "").replace("-", "")
    passcode   = str(body.get("passcode", "")).strip().upper()

    if not meeting_id or not passcode:
        return JsonResponse({"error": "Meeting ID and passcode are required."}, status=400)

    try:
        meeting = Meeting.objects.get(meeting_id=meeting_id)
    except Meeting.DoesNotExist:
        return JsonResponse({"error": "Meeting not found. Check your Meeting ID."}, status=404)

    # Allow joining if meeting is active OR it's scheduled (waiting room handles it)
    if not meeting.is_active and not meeting.is_scheduled and meeting.host != request.user:
        return JsonResponse({"error": "This meeting has not started yet."}, status=404)

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

    meeting.is_active = False
    meeting.activated_at = None  # Reset so the next session starts fresh
    meeting.save(update_fields=["is_active", "activated_at"])

    # Clean up waiting room knocks
    WaitingRoomKnock.objects.filter(meeting=meeting).delete()
    MeetingCapacityQueue.objects.filter(meeting=meeting).delete()

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
            except Exception:
                pass
            await _asyncio.sleep(0.5)
            try:
                await lk.room.delete_room(DeleteRoomRequest(room=room_name))
            except Exception:
                pass

    def _run_in_thread():
        import logging
        import asyncio as _asyncio
        logging.getLogger("asyncio").setLevel(logging.CRITICAL)
        logging.getLogger("asgiref").setLevel(logging.CRITICAL)
        loop = _asyncio.new_event_loop()
        _asyncio.set_event_loop(loop)
        try:
            loop.run_until_complete(_end_livekit())
        except Exception:
            pass
        finally:
            try:
                pending = _asyncio.all_tasks(loop)
                for task in pending:
                    task.cancel()
                if pending:
                    loop.run_until_complete(_asyncio.gather(*pending, return_exceptions=True))
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
# SCHEDULE MEETING  (updated — passes require_admission)
######################################################

@login_required
@require_POST
def schedule_meeting(request):
    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    title              = body.get("title", "Scheduled Meeting").strip() or "Scheduled Meeting"
    start_str          = body.get("start")
    end_str            = body.get("end")
    is_all_day         = body.get("all_day", False)
    repeat             = body.get("repeat", "none")
    repeat_end         = body.get("repeat_end")
    invitee_emails     = [e.strip().lower() for e in body.get("invitees", []) if e.strip()]
    require_admission  = bool(body.get("require_admission", False))   # ← NEW
    only_host_audio       = bool(body.get("only_host_audio", False))
    only_host_video       = bool(body.get("only_host_video", False))
    only_host_chat        = bool(body.get("only_host_chat", False))
    only_host_screenshare = bool(body.get("only_host_screenshare", False))

    room_name = secrets.token_hex(32)
    meeting = Meeting.objects.create(
        host=request.user,
        room_name=room_name,
        title=title,
        is_scheduled=True,
        is_all_day=is_all_day,
        repeat=repeat,
        require_admission=require_admission,  # ← NEW
        only_host_audio=only_host_audio,
        only_host_video=only_host_video,
        only_host_chat=only_host_chat,
        only_host_screenshare=only_host_screenshare,
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

    for email in invitee_emails:
        MeetingInvitee.objects.get_or_create(meeting=meeting, email=email)

    chat = MeetingChat.objects.create(meeting=meeting)
    MeetingChatMember.objects.create(chat=chat, user=request.user)
    from django.contrib.auth import get_user_model
    User = get_user_model()
    for email in invitee_emails:
        try:
            u = User.objects.get(email=email)
            MeetingChatMember.objects.get_or_create(chat=chat, user=u)
        except User.DoesNotExist:
            pass

    send_meeting_invite(meeting, invitee_emails)

    return JsonResponse({
        "status": "scheduled",
        "meeting_id": meeting.meeting_id,
        "passcode": meeting.passcode,
        "room_name": room_name,
        "meeting_url": meeting.meeting_url,
    })


######################################################
# CHAT VIEWS  (unchanged — kept for completeness)
######################################################

@login_required
def chat_list(request):
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

    member = MeetingChatMember.objects.filter(
        chat=chat, user=request.user, is_removed=False
    ).first()
    if not member:
        return JsonResponse({"error": "Not a member"}, status=403)

    msgs = chat.messages.exclude(
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
                "id":      m['id'],
                "sender":  m['sender__name'],
                "is_me":   m['sender_id'] == request.user.id,
                "text":    m['text'],
                "sent_at": m['sent_at'].isoformat(),
            }
            for m in msgs
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
    return render(request, "meetings/all_meetings.html")


@login_required
def scheduled_meetings_for_date(request):
    from datetime import date, timedelta

    date_str = request.GET.get("date")
    try:
        target_date = date.fromisoformat(date_str) if date_str else date.today()
    except ValueError:
        return JsonResponse({"error": "Invalid date"}, status=400)

    hosted  = Meeting.objects.filter(host=request.user, is_scheduled=True)
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
        rep_end   = m.repeat_end_date

        occurs = False
        if repeat == 'none':
            occurs = (orig_date == target_date)
        elif repeat == 'daily':
            occurs = (target_date >= orig_date and (rep_end is None or target_date <= rep_end))
        elif repeat == 'weekday':
            occurs = (target_date >= orig_date and target_date.weekday() < 5 and (rep_end is None or target_date <= rep_end))
        elif repeat == 'weekly':
            delta = (target_date - orig_date).days
            occurs = (target_date >= orig_date and delta % 7 == 0 and (rep_end is None or target_date <= rep_end))
        elif repeat == 'monthly':
            occurs = (target_date >= orig_date and target_date.day == orig_date.day and (rep_end is None or target_date <= rep_end))
        elif repeat == 'yearly':
            occurs = (target_date >= orig_date and target_date.day == orig_date.day and target_date.month == orig_date.month and (rep_end is None or target_date <= rep_end))

        if not occurs:
            continue

        if m.is_all_day or repeat == 'none':
            adj_start = m.scheduled_start
            adj_end   = m.scheduled_end
        else:
            from django.utils import timezone as tz
            def shift_to(dt, new_date):
                if dt is None: return None
                delta_days = (new_date - orig_date).days
                return dt + timedelta(days=delta_days)
            adj_start = shift_to(m.scheduled_start, target_date)
            adj_end   = shift_to(m.scheduled_end,   target_date)

        result.append({
            "id":             m.id,
            "title":          m.title,
            "meeting_id":     m.meeting_id,
            "passcode":       m.passcode,
            "room_name":      m.room_name,
            "meeting_url":    m.meeting_url,
            "start":          adj_start.isoformat() if adj_start else None,
            "end":            adj_end.isoformat()   if adj_end   else None,
            "original_start": m.scheduled_start.isoformat() if m.scheduled_start else None,
            "original_end":   m.scheduled_end.isoformat()   if m.scheduled_end   else None,
            "is_all_day":     m.is_all_day,
            "require_admission": m.require_admission,
            "repeat":         m.repeat,
            "repeat_end_date": m.repeat_end_date.isoformat() if m.repeat_end_date else None,
            "is_host":        m.host == request.user,
            "is_active":      m.is_active,
            "only_host_audio": m.only_host_audio,
            "only_host_video": m.only_host_video,
            "only_host_chat": m.only_host_chat,
            "only_host_screenshare": m.only_host_screenshare,
            "invitees":       list(m.invitees.values_list('email', flat=True)),
        })

    result.sort(key=lambda x: x['start'] or '')
    return JsonResponse({"meetings": result, "date": target_date.isoformat()})


@login_required
def all_scheduled_meetings(request):
    hosted  = Meeting.objects.filter(host=request.user, is_scheduled=True)
    invited = Meeting.objects.filter(is_scheduled=True, invitees__email=request.user.email).exclude(host=request.user)
    all_meetings = (hosted | invited).distinct().order_by('scheduled_start')

    result = []
    for m in all_meetings:
        result.append({
            "id":             m.id,
            "title":          m.title,
            "meeting_id":     m.meeting_id,
            "passcode":       m.passcode,
            "room_name":      m.room_name,
            "meeting_url":    m.meeting_url,
            "start":          m.scheduled_start.isoformat() if m.scheduled_start else None,
            "end":            m.scheduled_end.isoformat()   if m.scheduled_end   else None,
            "is_all_day":     m.is_all_day,
            "require_admission": m.require_admission,
            "repeat":         m.repeat,
            "repeat_end_date": m.repeat_end_date.isoformat() if m.repeat_end_date else None,
            "is_host":        m.host == request.user,
            "is_active":      m.is_active,
            "only_host_audio": m.only_host_audio,
            "only_host_video": m.only_host_video,
            "only_host_chat": m.only_host_chat,
            "only_host_screenshare": m.only_host_screenshare,
            "invitees":       list(m.invitees.values_list('email', flat=True)),
        })

    return JsonResponse({"meetings": result})


@login_required
@require_POST
def edit_meeting(request, meeting_id):
    meeting = get_object_or_404(Meeting, id=meeting_id, host=request.user)

    try:
        body = json.loads(request.body)
    except Exception:
        return JsonResponse({"error": "Invalid JSON"}, status=400)

    title             = body.get("title", meeting.title).strip() or meeting.title
    start_str         = body.get("start")
    end_str           = body.get("end")
    is_all_day        = body.get("all_day", meeting.is_all_day)
    repeat            = body.get("repeat", meeting.repeat)
    repeat_end        = body.get("repeat_end")
    invitee_emails    = [e.strip().lower() for e in body.get("invitees", []) if e.strip()]
    require_admission = body.get("require_admission", meeting.require_admission)  # ← NEW
    only_host_audio       = body.get("only_host_audio", meeting.only_host_audio)
    only_host_video       = body.get("only_host_video", meeting.only_host_video)
    only_host_chat        = body.get("only_host_chat", meeting.only_host_chat)
    only_host_screenshare = body.get("only_host_screenshare", meeting.only_host_screenshare)

    meeting.title             = title
    meeting.is_all_day        = is_all_day
    meeting.repeat            = repeat
    meeting.require_admission = bool(require_admission)  # ← NEW
    meeting.only_host_audio = bool(only_host_audio)
    meeting.only_host_video = bool(only_host_video)
    meeting.only_host_chat = bool(only_host_chat)
    meeting.only_host_screenshare = bool(only_host_screenshare)

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

    meeting.invitees.all().delete()
    for email in invitee_emails:
        MeetingInvitee.objects.get_or_create(meeting=meeting, email=email)

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
    if meeting and meeting.is_active and not meeting.activated_at:
        # Only deactivate if host left before the meeting was ever started
        meeting.is_active = False
        meeting.save(update_fields=['is_active'])
    return JsonResponse({"status": "ok"})


@login_required
@require_POST
def delete_meeting(request, meeting_id):
    meeting = get_object_or_404(Meeting, id=meeting_id, host=request.user)
    meeting.delete()
    return JsonResponse({"status": "deleted"})


@login_required
def chat_poll(request, chat_id):
    chat   = get_object_or_404(MeetingChat, id=chat_id)
    member = MeetingChatMember.objects.filter(chat=chat, user=request.user, is_removed=False).first()
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

    msgs = qs.order_by("sent_at").values("id", "sender__name", "sender_id", "text", "sent_at")
    return JsonResponse({
        "messages": [
            {"id": m["id"], "sender": m["sender__name"], "is_me": m["sender_id"] == request.user.id,
             "text": m["text"], "sent_at": m["sent_at"].isoformat()}
            for m in msgs
        ]
    })


@login_required
def chat_page(request):
    return render(request, "meetings/chat.html")


@login_required
def dm_list(request):
    participations = DirectChatParticipant.objects.filter(user=request.user).select_related('chat')
    result = []
    for p in participations:
        dm    = p.chat
        other = dm.participations.exclude(user=request.user).select_related('user').first()
        if not other: continue
        last = dm.messages.filter(is_deleted=False).order_by('-sent_at').first()
        result.append({
            "dm_id":        dm.id,
            "other_user":   {"id": other.user.id, "name": other.user.name, "email": other.user.email},
            "last_message": last.text if last else "",
            "last_at":      last.sent_at.isoformat() if last else "",
        })
    result.sort(key=lambda x: x["last_at"] or "", reverse=True)
    return JsonResponse({"dms": result})


@login_required
@require_POST
def dm_get_or_create(request):
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

    my_dm_ids    = DirectChatParticipant.objects.filter(user=request.user).values_list('chat_id', flat=True)
    other_dm_ids = DirectChatParticipant.objects.filter(user=other_user).values_list('chat_id', flat=True)
    shared       = set(my_dm_ids) & set(other_dm_ids)

    if shared:
        return JsonResponse({"dm_id": list(shared)[0], "created": False})

    dm = DirectChat.objects.create()
    DirectChatParticipant.objects.create(chat=dm, user=request.user)
    DirectChatParticipant.objects.create(chat=dm, user=other_user)
    return JsonResponse({"dm_id": dm.id, "created": True})


@login_required
def dm_messages(request, dm_id):
    dm = get_object_or_404(DirectChat, id=dm_id)
    if not dm.participations.filter(user=request.user).exists():
        return JsonResponse({"error": "Not a participant"}, status=403)

    since = request.GET.get("since")
    qs    = dm.messages.filter(is_deleted=False)
    if since:
        try:
            from django.utils.dateparse import parse_datetime as _pd
            dt = _pd(since)
            if dt: qs = qs.filter(sent_at__gt=dt)
        except Exception:
            pass

    msgs  = qs.order_by("sent_at").select_related("sender")
    other = dm.participations.exclude(user=request.user).select_related("user").first()
    return JsonResponse({
        "messages": [
            {"id": m.id, "sender": m.sender.name, "is_me": m.sender_id == request.user.id,
             "text": m.text, "sent_at": m.sent_at.isoformat()}
            for m in msgs
        ],
        "other_user": {"id": other.user.id, "name": other.user.name} if other else None,
    })


@login_required
@require_POST
def dm_send(request, dm_id):
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


@login_required
def search_users(request):
    q = request.GET.get("q", "").strip()
    if len(q) < 2:
        return JsonResponse({"users": []})

    from django.contrib.auth import get_user_model
    from django.db.models import Q
    User = get_user_model()
    users = User.objects.filter(Q(name__icontains=q) | Q(email__icontains=q)).exclude(pk=request.user.pk)[:10]
    return JsonResponse({"users": [{"id": u.id, "name": u.name, "email": u.email} for u in users]})


@login_required
def settings_page(request):
    user = request.user

    if request.method == "POST":
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

        if "remove_photo" in request.POST:
            if user.user_profile_picture:
                user.user_profile_picture.delete(save=False)
                user.user_profile_picture = None
                user.save()
            return redirect("meetings:settings_page")

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

    profile_image_url = None
    if user.user_profile_picture:
        try:
            profile_image_url = user.user_profile_picture.url
        except Exception:
            profile_image_url = None

    return render(request, "meetings/settings.html", {"profile_image_url": profile_image_url})


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
        transcript = MeetingTranscript.objects.create(meeting=meeting, file=file, duration_seconds=duration)
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





######################################################
# SUBSCRIPTION HELPERS
######################################################

def _get_or_create_subscription(user):
    """
    Returns the user's UserSubscription, creating a Free-plan one if absent.
    Never raises — always returns something usable.
    """
    try:
        sub = user.subscription
        return sub
    except UserSubscription.DoesNotExist:
        pass

    # Auto-provision a Free subscription
    try:
        free_plan = SubscriptionPlan.objects.get(name='free')
    except SubscriptionPlan.DoesNotExist:
        # Plans haven't been seeded yet — return synthetic defaults
        return None

    sub = UserSubscription.objects.create(user=user, plan=free_plan, is_active=True)
    return sub


def _get_plan_limits(user):
    """
    Returns (max_participants: int, max_duration_minutes: int) for the user.
    Falls back to the tightest Free-plan defaults (100 / 40) if anything is wrong.
    """
    DEFAULT_PARTICIPANTS = 100
    DEFAULT_DURATION     = 40

    sub = _get_or_create_subscription(user)
    if sub is None:
        return DEFAULT_PARTICIPANTS, DEFAULT_DURATION

    plan = sub.effective_plan
    return plan.max_participants, plan.max_duration_minutes


def _get_plan_features(user):
    """Returns a dict of boolean feature flags for the user's active plan."""
    sub = _get_or_create_subscription(user)
    if sub is None:
        return {"can_record": False, "can_use_waiting_room": False, "can_schedule": False}
    plan = sub.effective_plan
    return {
        "can_record":           plan.can_record,
        "can_use_waiting_room": plan.can_use_waiting_room,
        "can_schedule":         plan.can_schedule,
    }