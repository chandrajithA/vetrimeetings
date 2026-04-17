from django.contrib import admin
from .models import *

# Register your models here.
admin.site.register(Meeting)
admin.site.register(MeetingRecording)
admin.site.register(MeetingInvitee)
admin.site.register(MeetingChatMessage)
admin.site.register(MeetingChat)
admin.site.register(MeetingChatMember)