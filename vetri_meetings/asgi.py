# import os
# import django
# from channels.routing import ProtocolTypeRouter, URLRouter
# from django.core.asgi import get_asgi_application
# from channels.auth import AuthMiddlewareStack
# import meetings.routing

# os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vetri_meetings.settings')

# django.setup()

# application = ProtocolTypeRouter({
#     "http": get_asgi_application(),   # HTTP handled by Django

#     "websocket": AuthMiddlewareStack(
#         URLRouter(
#             meetings.routing.websocket_urlpatterns
#         )
#     ),
# })



# asgi.py
import os
import django
from channels.routing import ProtocolTypeRouter, URLRouter
from django.core.asgi import get_asgi_application
from channels.auth import AuthMiddlewareStack

os.environ.setdefault('DJANGO_SETTINGS_MODULE', 'vetri_meetings.settings')
django.setup()

# ✅ Empty websocket patterns — just stops the error
from meetings.routing import websocket_urlpatterns

application = ProtocolTypeRouter({
    "http": get_asgi_application(),
    "websocket": AuthMiddlewareStack(
        URLRouter(websocket_urlpatterns)
    ),
})