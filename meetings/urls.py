from django.urls import path
from .views import *

app_name = 'meetings'

urlpatterns = [
    path("", dashboard, name="dashboard"),
    path("meeting/create/", create_meeting, name="create_meeting"),
    path("room/<str:room_name>/",room, name="meeting_room"),
    path('meeting/save-recording/', save_recording, name='save_recording'),
    path('meetings/', all_meetings_page, name='all_meetings'),
    path("meeting/join/", join_by_id, name="join_by_id"),

    # API endpoints
    path("meeting/token/<str:room_name>/", get_token, name="meeting_token"),
    path("meeting/end/<str:room_name>/", end_meeting, name="meeting_end"),
    path("participants/<str:room_name>/", participants, name="meeting_participants"),
    
    # ... existing urls ...
    path('meeting/schedule/', schedule_meeting, name='schedule_meeting'),
    path('meeting/scheduled/', scheduled_meetings_for_date, name='scheduled_meetings_for_date'),
    path('meeting/all-scheduled/', all_scheduled_meetings, name='all_scheduled_meetings'),
    path('meeting/edit/<int:meeting_id>/', edit_meeting, name='edit_meeting'),
    path('meeting/delete/<int:meeting_id>/', delete_meeting, name='delete_meeting'),
    
    # Chat page
    path('chat/', chat_page, name='chat_page'),

    # Meeting group-chat endpoints
    path('meeting/chat/list/', chat_list, name='chat_list'),
    path('meeting/chat/<int:chat_id>/messages/', chat_messages, name='chat_messages'),
    path('meeting/chat/<int:chat_id>/send/', chat_send, name='chat_send'),
    path('meeting/chat/<int:chat_id>/poll/', chat_poll, name='chat_poll'),   
    path('meeting/chat/<int:chat_id>/delete/', chat_delete_for_me, name='chat_delete'),
    path('meeting/chat/<int:chat_id>/add-member/', chat_add_member, name='chat_add_member'),
    path('meeting/chat/<int:chat_id>/remove-member/<int:user_id>/', chat_remove_member, name='chat_remove_member'),

    # Direct messages
    path('meeting/dm/', dm_list, name='dm_list'),        
    path('meeting/dm/get-or-create/', dm_get_or_create, name='dm_get_or_create'),
    path('meeting/dm/<int:dm_id>/', dm_messages, name='dm_messages'),     
    path('meeting/dm/<int:dm_id>/send/', dm_send, name='dm_send'),       

    # User search
    path('users/search/', search_users, name='search_users'),

]
