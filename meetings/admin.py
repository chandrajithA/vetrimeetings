from django.contrib import admin
from .models import Meeting, MeetingRecording

# Register your models here.
admin.site.register(Meeting)
admin.site.register(MeetingRecording)