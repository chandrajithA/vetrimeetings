from django.db import models
import uuid
import random
import string
from django.conf import settings

User = settings.AUTH_USER_MODEL

def _generate_meeting_id():
    """12-digit unique numeric meeting ID, e.g. 482910374651"""
    return ''.join([str(random.randint(0, 9)) for _ in range(12)])


def _generate_passcode():
    """8-character alphanumeric passcode (uppercase + digits), e.g. A3K9PZ2W"""
    chars = string.ascii_uppercase + string.digits
    return ''.join(random.choices(chars, k=8))


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
    is_active   = models.BooleanField(default=True)
    # ── Scheduling ──
    scheduled_start = models.DateTimeField(blank=True, null=True)
    scheduled_end   = models.DateTimeField(blank=True, null=True)
    is_all_day      = models.BooleanField(default=False)
    repeat          = models.CharField(max_length=10, choices=REPEAT_CHOICES, default='none')
    repeat_end_date = models.DateField(blank=True, null=True)
    is_scheduled    = models.BooleanField(default=False)
    
    created_at  = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"{self.title} ({self.room_name})"

    def save(self, *args, **kwargs):
        if not self.room_name:
            self.room_name = uuid.uuid4().hex[:10]
        if not self.meeting_id:
            # Ensure uniqueness
            mid = _generate_meeting_id()
            while Meeting.objects.filter(meeting_id=mid).exists():
                mid = _generate_meeting_id()
            self.meeting_id = mid
        if not self.passcode:
            self.passcode = _generate_passcode()
        super().save(*args, **kwargs)
        
        
class MeetingInvitee(models.Model):
    meeting = models.ForeignKey(Meeting, on_delete=models.CASCADE, related_name='invitees')
    email   = models.EmailField()
    added_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        unique_together = ('meeting', 'email')

    def __str__(self):
        return self.email
        
        
class MeetingRecording(models.Model):
    meeting = models.ForeignKey('Meeting', on_delete=models.CASCADE, related_name='recordings')
    file    = models.FileField(upload_to='recordings/')
    recorded_at = models.DateTimeField(auto_now_add=True)

    def __str__(self):
        return f"Recording for {self.meeting} at {self.recorded_at}"
    
    
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