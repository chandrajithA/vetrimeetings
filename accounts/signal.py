from django.contrib.auth.signals import user_logged_in
from django.dispatch import receiver
from .models import UserLoginActivity

@receiver(user_logged_in)
def log_user_login(sender, request, user, **kwargs):
    UserLoginActivity.objects.create(user=user,ip_address=get_client_ip(request))


def get_client_ip(request):
    xff = request.META.get("HTTP_X_FORWARDED_FOR")
    if xff:
        return xff.split(",")[0]
    return request.META.get("REMOTE_ADDR")