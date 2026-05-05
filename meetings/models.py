from django.db import models
import uuid
import random
import string
from django.conf import settings
from django.utils import timezone

User = settings.AUTH_USER_MODEL

def _generate_meeting_id():
    """12-digit unique numeric meeting ID, e.g. 482910374651"""
    return ''.join([str(random.randint(0, 9)) for _ in range(12)])


def _generate_passcode():
    """8-character alphanumeric passcode (uppercase + digits), e.g. A3K9PZ2W"""
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choices(chars, k=8))



# ══════════════════════════════════════════════════════════════════════════════
# SUBSCRIPTION SYSTEM
# ══════════════════════════════════════════════════════════════════════════════

class SubscriptionPlan(models.Model):
    """
    Defines the tiers available (Free / Basic / Premium).
    Seed via: python manage.py create_subscription_plans
    """

    PLAN_CHOICES = [
        ('free',    'Free'),
        ('basic',   'Basic'),
        ('premium', 'Premium'),
    ]

    name         = models.CharField(max_length=20, choices=PLAN_CHOICES, unique=True)
    display_name = models.CharField(max_length=50)
    description  = models.TextField(blank=True, default="")

    # ── Hard limits ──────────────────────────────────────────────────────────
    # 0 = unlimited
    max_duration_minutes = models.PositiveIntegerField(
        default=40,
        help_text="Maximum meeting duration in minutes. 0 = unlimited.",
    )
    max_participants = models.PositiveIntegerField(
        default=100,
        help_text="Maximum simultaneous participants per meeting.",
    )

    # ── Feature flags ────────────────────────────────────────────────────────
    can_record          = models.BooleanField(default=False)
    can_use_waiting_room = models.BooleanField(default=False)
    can_schedule        = models.BooleanField(default=False)

    # ── Pricing (informational) ──────────────────────────────────────────────
    price_monthly = models.DecimalField(
        max_digits=8, decimal_places=2, default=0.00,
        help_text="Monthly price in USD (for display).",
    )

    class Meta:
        ordering = ['price_monthly']

    def __str__(self):
        return f"{self.display_name} (max {self.max_participants} participants, " \
               f"{'unlimited' if self.max_duration_minutes == 0 else str(self.max_duration_minutes) + ' min'})"

    @property
    def is_unlimited_duration(self):
        return self.max_duration_minutes == 0

    @property
    def is_unlimited_participants(self):
        return self.max_participants == 0


class UserSubscription(models.Model):
    """
    One row per user — links a user to their active SubscriptionPlan.
    Created automatically (Free plan) the first time it is needed.
    """

    user       = models.OneToOneField(User, on_delete=models.CASCADE, related_name='subscription')
    plan       = models.ForeignKey(SubscriptionPlan, on_delete=models.PROTECT, related_name='subscribers')
    started_at = models.DateTimeField(auto_now_add=True)
    expires_at = models.DateTimeField(null=True, blank=True,
                                      help_text="Null = never expires (lifetime or free).")
    is_active  = models.BooleanField(default=True)

    class Meta:
        verbose_name        = "User Subscription"
        verbose_name_plural = "User Subscriptions"

    def __str__(self):
        return f"{self.user} → {self.plan.display_name}"

    @property
    def is_valid(self):
        if not self.is_active:
            return False
        if self.expires_at and self.expires_at < timezone.now():
            return False
        return True

    @property
    def effective_plan(self):
        """Returns the plan if valid, otherwise falls back to the free plan."""
        if self.is_valid:
            return self.plan
        try:
            return SubscriptionPlan.objects.get(name='free')
        except SubscriptionPlan.DoesNotExist:
            return self.plan
        
        
# ══════════════════════════════════════════════════════════════════════════════
# MEETING
# ══════════════════════════════════════════════════════════════════════════════


class Meeting(models.Model):
    
    REPEAT_CHOICES = [
        ('none',    'Does not repeat'),
        ('daily',   'Daily'),
        ('weekday', 'Every weekday (Mon–Fri)'),
        ('weekly',  'Weekly'),
        ('monthly', 'Monthly'),
        ('yearly',  'Yearly'),
    ]
    
    title       = models.CharField(max_length=200, default="Quick Meeting")
    host        = models.ForeignKey(User, on_delete=models.CASCADE, related_name="hosted_meetings")
    room_name   = models.CharField(max_length=100, unique=True)
    meeting_id  = models.CharField(max_length=12, unique=True, blank=True)
    passcode     = models.CharField(max_length=8, blank=True) 
    meeting_url = models.URLField(max_length=500, blank=True, default="")
    is_active   = models.BooleanField(default=False)
    # ── Scheduling ──
    require_admission = models.BooleanField(default=False)
    scheduled_start = models.DateTimeField(blank=True, null=True)
    scheduled_end   = models.DateTimeField(blank=True, null=True)
    is_all_day      = models.BooleanField(default=False)
    repeat          = models.CharField(max_length=10, choices=REPEAT_CHOICES, default='none')
    repeat_end_date = models.DateField(blank=True, null=True)
    is_scheduled    = models.BooleanField(default=False)
    
    # ── Subscription-derived limits (snapshotted at creation time) ────────────
    # Snapshotting means a plan downgrade won't cut short a meeting already
    # in progress, and old meeting records preserve their original limits.
    max_participants     = models.PositiveIntegerField(
        default=100,
        help_text="Max simultaneous participants (copied from host's plan at creation).",
    )
    max_duration_minutes = models.PositiveIntegerField(
        default=40,
        help_text="Max meeting duration in minutes. 0 = unlimited.",
    )

    # ── Runtime tracking ─────────────────────────────────────────────────────
    activated_at      = models.DateTimeField(
        null=True, blank=True,
        help_text="When the host first activated (started) the meeting.",
    )
    
    only_host_audio       = models.BooleanField(
        default=False,
        help_text="Only host can speak; participant mics are disabled.",
    )
    only_host_video       = models.BooleanField(
        default=False,
        help_text="Participants cannot turn on their cameras.",
    )
    only_host_chat        = models.BooleanField(
        default=False,
        help_text="Participants cannot send chat messages (read-only).",
    )
    only_host_screenshare = models.BooleanField(
        default=False,
        help_text="Participants cannot share their screen.",
    )
    
    created_at  = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} ({self.room_name})"

    def __str__(self):
        return f"{self.title} ({self.room_name})"

    def save(self, *args, **kwargs):
        if not self.room_name:
            self.room_name = uuid.uuid4().hex[:10]
        if not self.meeting_id:
            mid = _generate_meeting_id()
            while Meeting.objects.filter(meeting_id=mid).exists():
                mid = _generate_meeting_id()
            self.meeting_id = mid
        if not self.passcode:
            self.passcode = _generate_passcode()
        super().save(*args, **kwargs)
        
        
    # ── Derived helpers ───────────────────────────────────────────────────────

    @property
    def seconds_remaining(self):
        """
        Returns (int) seconds left, or None if unlimited or not yet started.
        Negative value means the meeting has already overrun.
        """
        if self.max_duration_minutes == 0 or not self.activated_at:
            return None
        elapsed = (timezone.now() - self.activated_at).total_seconds()
        return int(self.max_duration_minutes * 60 - elapsed)

    @property
    def is_time_limited(self):
        return self.max_duration_minutes > 0

    @property
    def duration_limit_display(self):
        if self.max_duration_minutes == 0:
            return "Unlimited"
        h, m = divmod(self.max_duration_minutes, 60)
        return f"{h}h {m:02d}m" if h else f"{m} min"
    
    
# ══════════════════════════════════════════════════════════════════════════════
# REST OF MODELS (unchanged)
# ══════════════════════════════════════════════════════════════════════════════
        
        
class MeetingInvitee(models.Model):
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name='invitees')
    email   = models.EmailField()
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('meeting', 'email')

    def __str__(self):
        return self.email
    
    
    
class WaitingRoomKnock(models.Model):
    """
    Tracks users waiting to be admitted into a meeting that requires admission.
    One row per user per meeting.  Status transitions:
        waiting → admitted  (host clicked Admit)
        waiting → denied    (host clicked Deny)
    """
    STATUS_CHOICES = [
        ('waiting',  'Waiting'),
        ('admitted', 'Admitted'),
        ('denied',   'Denied'),
    ]

    meeting      = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name='knocks')
    user         = models.ForeignKey(User, on_delete=models.CASCADE, related_name='knocks')
    display_name = models.CharField(max_length=120, blank=True)
    status       = models.CharField(max_length=10, choices=STATUS_CHOICES, default='waiting')
    created_at   = models.DateTimeField(auto_now_add=True)
    updated_at   = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('meeting', 'user')

    def __str__(self):
        return f"{self.display_name or self.user} → {self.meeting} [{self.status}]"
        
        
class MeetingRecording(models.Model):
    meeting = models.ForeignKey('Meeting', on_delete=models.CASCADE, related_name='recordings')
    file    = models.FileField(upload_to='recordings/')
    thumbnail   = models.ImageField(upload_to='recording_thumbs/', blank=True, null=True)
    duration_seconds = models.PositiveIntegerField(default=0)   # 
    recorded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Recording for {self.meeting} at {self.recorded_at}"
    
    @property
    def duration_display(self):
        s = self.duration_seconds
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h {m:02d}m {sec:02d}s"
        return f"{m:02d}:{sec:02d}"
    
    
class MeetingChat(models.Model):
    """One chat thread per meeting — persists forever."""
    meeting  = models.OneToOneField(Meeting, on_delete=models.CASCADE, related_name='chat')
    members  = models.ManyToManyField(settings.AUTH_USER_MODEL, through='MeetingChatMember', related_name='meeting_chats')
    created_at = models.DateTimeField(auto_now_add=True)


class MeetingChatMember(models.Model):
    chat       = models.ForeignKey(MeetingChat, on_delete=models.CASCADE, related_name='memberships')
    user       = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    is_removed = models.BooleanField(default=False)   # host removed them
    deleted_for_self = models.BooleanField(default=False)  # user hid the chat
    joined_at  = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('chat', 'user')


class MeetingChatMessage(models.Model):
    chat      = models.ForeignKey(MeetingChat, on_delete=models.CASCADE, related_name='messages')
    sender    = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    text      = models.TextField()
    sent_at   = models.DateTimeField(auto_now_add=True)
    deleted_by = models.ManyToManyField(settings.AUTH_USER_MODEL, related_name='deleted_messages', blank=True)

    def __str__(self):
        return f"{self.sender} @ {self.sent_at:%H:%M}: {self.text[:40]}"
    
    
class DirectChat(models.Model):
    """One-on-one direct message thread between two users."""
    created_at = models.DateTimeField(auto_now_add=True)

    def get_other_user(self, user):
        p = self.participations.exclude(user=user).select_related('user').first()
        return p.user if p else None

    def __str__(self):
        names = [p.user.name for p in self.participations.select_related('user').all()]
        return f"DM: {' ↔ '.join(names)}"


class DirectChatParticipant(models.Model):
    chat      = models.ForeignKey(DirectChat, on_delete=models.CASCADE, related_name='participations')
    user      = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    joined_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('chat', 'user')


class DirectChatMessage(models.Model):
    chat       = models.ForeignKey(DirectChat, on_delete=models.CASCADE, related_name='messages')
    sender     = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    text       = models.TextField()
    sent_at    = models.DateTimeField(auto_now_add=True)
    is_deleted = models.BooleanField(default=False)
    is_read    = models.BooleanField(default=False, db_index=True)

    def __str__(self):
        return f"{self.sender} @ {self.sent_at:%H:%M}: {self.text[:40]}"
    
    
    
class MeetingTranscript(models.Model):
    meeting     = models.ForeignKey('Meeting', on_delete=models.CASCADE, related_name='transcripts')
    file        = models.FileField(upload_to='transcripts/')
    duration_seconds = models.PositiveIntegerField(default=0)
    recorded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Transcript for {self.meeting} at {self.recorded_at}"

    @property
    def duration_display(self):
        s = self.duration_seconds
        h, rem = divmod(s, 3600)
        m, sec = divmod(rem, 60)
        if h:
            return f"{h}h {m:02d}m {sec:02d}s"
        return f"{m:02d}:{sec:02d}"
    
    
class UserChatRead(models.Model):
    """Tracks when a user last read a group chat."""
    user        = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    chat        = models.ForeignKey('MeetingChat', on_delete=models.CASCADE)
    last_read_at = models.DateTimeField(auto_now=True)

    class Meta:
        unique_together = ('user', 'chat')
        indexes = [models.Index(fields=['user', 'chat'])]
        
        
        
class MeetingCapacityQueue(models.Model):
    """
    Tracks users who tried to join but the meeting was at capacity.
    Ordered by created_at — first-come-first-served.
    """
    meeting      = models.ForeignKey('Meeting', on_delete=models.CASCADE, related_name='capacity_queue')
    user         = models.ForeignKey(settings.AUTH_USER_MODEL, on_delete=models.CASCADE)
    display_name = models.CharField(max_length=120, blank=True)
    created_at   = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('meeting', 'user')
        ordering        = ['created_at']

    def __str__(self):
        return f"{self.display_name or self.user} queued for {self.meeting}"