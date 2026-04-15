# from channels.generic.websocket import AsyncWebsocketConsumer
# import json

# class MeetingConsumer(AsyncWebsocketConsumer):

#     async def connect(self):
#         self.room_name = self.scope['url_route']['kwargs']['room_name']
#         self.room_group_name = f"room_{self.room_name}"

#         self.user_id = None

#         await self.channel_layer.group_add(
#             self.room_group_name,
#             self.channel_name
#         )

#         await self.accept()

#     async def receive(self, text_data):
#         data = json.loads(text_data)

#         if data["type"] == "join":
#             self.user_id = data["user_id"]

#             # 🔥 notify ALL users
#             await self.channel_layer.group_send(
#                 self.room_group_name,
#                 {
#                     "type": "signal_message",
#                     "message": {
#                         "type": "new_user",
#                         "from": self.user_id
#                     }
#                 }
#             )
#             return

#         # 🔥 forward all signaling
#         await self.channel_layer.group_send(
#             self.room_group_name,
#             {
#                 "type": "signal_message",
#                 "message": data
#             }
#         )

#     async def signal_message(self, event):
#         await self.send(text_data=json.dumps(event["message"]))
        
#     async def disconnect(self, close_code):
#         if self.user_id:
#             await self.channel_layer.group_send(
#                 self.room_group_name,
#                 {
#                     "type": "signal_message",
#                     "message": {
#                         "type": "user_left",
#                         "from": self.user_id
#                     }
#                 }
#             )





# consumers.py
from channels.generic.websocket import AsyncWebsocketConsumer
import json

class MeetingConsumer(AsyncWebsocketConsumer):

    async def connect(self):
        self.room_name = self.scope['url_route']['kwargs']['room_name']
        self.room_group_name = f"room_{self.room_name}"

        # ✅ Use Django's authenticated user ID — stable across refreshes
        user = self.scope.get("user")
        if user and user.is_authenticated:
            self.user_id = str(user.id)
        else:
            await self.close()
            return

        await self.channel_layer.group_add(
            self.room_group_name,
            self.channel_name
        )
        await self.accept()

        # ✅ Notify others this user joined
        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "signal_message",
                "message": {
                    "type": "new_user",
                    "from": self.user_id
                }
            }
        )

    async def receive(self, text_data):
        data = json.loads(text_data)

        # ✅ Always stamp the real user_id from session — never trust frontend
        data["from"] = self.user_id

        await self.channel_layer.group_send(
            self.room_group_name,
            {
                "type": "signal_message",
                "message": data
            }
        )

    async def signal_message(self, event):
        await self.send(text_data=json.dumps(event["message"]))

    async def disconnect(self, close_code):
        if hasattr(self, 'user_id') and self.user_id:
            # ✅ Remove from group FIRST, then broadcast — prevents echo
            await self.channel_layer.group_discard(
                self.room_group_name,
                self.channel_name
            )
            await self.channel_layer.group_send(
                self.room_group_name,
                {
                    "type": "signal_message",
                    "message": {
                        "type": "user_left",
                        "from": self.user_id
                    }
                }
            )