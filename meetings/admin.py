from django.contrib import admin
from .models import *

admin.site.register(UserSubscription)
admin.site.register(SubscriptionPlan)



# # ==============================
# # 📅 MEETING INLINE COMPONENTS
# # ==============================
class MeetingInviteeInline(admin.TabularInline):
    model = MeetingInvitee
    extra = 0


@admin.register(Meeting)
class MeetingAdmin(admin.ModelAdmin):
    list_display = (
        "title", "host", "meeting_id",
        "is_active", "is_scheduled", "scheduled_start"
    )

    list_filter = ("is_active", "is_scheduled", "repeat")
    search_fields = ("title", "meeting_id", "host__email")

    # ✅ ONLY VALID INLINE
    inlines = [MeetingInviteeInline]

    readonly_fields = ("meeting_id", "passcode", "meeting_url", "created_at")

    ordering = ("-created_at",)


# # ==============================
# # 💬 CHAT ADMIN (CORRECT PLACE)
# # ==============================
# class MeetingChatMemberInline(admin.TabularInline):
#     model = MeetingChatMember
#     extra = 0


# class MeetingChatMessageInline(admin.TabularInline):
#     model = MeetingChatMessage
#     extra = 0
#     readonly_fields = ("sender", "text", "sent_at")
#     ordering = ("-sent_at",)


# @admin.register(MeetingChat)
# class MeetingChatAdmin(admin.ModelAdmin):
#     list_display = ("meeting", "created_at")
#     inlines = [MeetingChatMemberInline, MeetingChatMessageInline]
#     search_fields = ("meeting__title",)


# @admin.register(MeetingChatMessage)
# class MeetingChatMessageAdmin(admin.ModelAdmin):
#     list_display = ("chat", "sender", "short_text", "sent_at")
#     search_fields = ("sender__email", "text")
#     ordering = ("-sent_at",)

#     def short_text(self, obj):
#         return obj.text[:50]


# # ==============================
# # 🎥 RECORDINGS
# # ==============================
@admin.register(MeetingRecording)
class MeetingRecordingAdmin(admin.ModelAdmin):
    list_display = ("meeting", "recorded_at")
    search_fields = ("meeting__title",)
    ordering = ("-recorded_at",)


# # ==============================
# # 💬 DIRECT CHAT (DM)
# # ==============================
# class DirectChatParticipantInline(admin.TabularInline):
#     model = DirectChatParticipant
#     extra = 0


# @admin.register(DirectChat)
# class DirectChatAdmin(admin.ModelAdmin):
#     list_display = ("id", "created_at")
#     inlines = [DirectChatParticipantInline]


# @admin.register(DirectChatMessage)
# class DirectChatMessageAdmin(admin.ModelAdmin):
#     list_display = ("chat", "sender", "short_text", "sent_at", "is_deleted")
#     search_fields = ("sender__email", "text")
#     ordering = ("-sent_at",)

#     def short_text(self, obj):
#         return obj.text[:50]