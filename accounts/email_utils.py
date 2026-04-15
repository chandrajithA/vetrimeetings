from django.conf import settings
from django.core.mail import get_connection

def get_email_connection():

    return get_connection(
        EMAIL_BACKEND=settings.EMAIL_BACKEND, 
        EMAIL_HOST=settings.EMAIL_HOST, 
        EMAIL_PORT=settings.EMAIL_PORT, 
        EMAIL_HOST_USER=settings.EMAIL_HOST_USER, 
        EMAIL_HOST_PASSWORD=settings.EMAIL_HOST_PASSWORD, 
        EMAIL_USE_TLS=settings.EMAIL_USE_TLS, 
        DEFAULT_FROM_EMAIL=settings.DEFAULT_FROM_EMAIL,
        fail_silently=False
    )