from django.core.cache import cache
from django.core.exceptions import ValidationError
from django.contrib.auth import get_user_model
from django.contrib.auth.forms import PasswordResetForm
from django.template.loader import render_to_string
from django.utils.http import urlsafe_base64_encode
from django.utils.encoding import force_bytes
from django.utils.translation import gettext_lazy as _
from django.core.mail import send_mail, get_connection
from .email_utils import get_email_connection
from decouple import config
from django.conf import settings

User = get_user_model()

class RateLimitedPasswordResetForm(PasswordResetForm):
    def __init__(self, *args, **kwargs):
        self.request = kwargs.pop('request', None)  # Safely extract `request`
        super().__init__(*args, **kwargs)

    def clean_email(self):
        email = self.cleaned_data['email']
        request = self.request  # passed from view

        # IP-based rate limit
        ip = self._get_ip(request)
        ip_key_hour = f"reset_ip_hour:{ip}"
        ip_hour_count = cache.get(ip_key_hour, 0)

        if ip_hour_count >= 20:
            raise ValidationError(_("Too many password reset attempts from your IP. Try again later."))

        self.key_hour = f"reset_hourly_limit:{email}"
        self.key_day = f"reset_daily_count:{email}"
        self.key_ip = ip_key_hour  # store for use in save()

        # Email-based rate limit
        hour_count = cache.get(self.key_hour, 0)
        if hour_count >= 10:
            raise ValidationError(_("You can only request a password reset 3 times per hour."))

        daily_count = cache.get(self.key_day, 0)
        if daily_count >= 15:
            raise ValidationError(_("You've exceeded the daily limit for password reset requests."))

        return email

    def save(self, domain_override=None,
             subject_template_name='accounts/password_reset_subject.txt',
             email_template_name='accounts/password_reset_email.html',
             use_https=False, token_generator=None,
             from_email=None, request=None, html_email_template_name=None,
             extra_email_context=None):

        email = self.cleaned_data['email']
        ip_key_hour = self.key_ip

        # Update rate limit counts
        current_hour_count = cache.get(self.key_hour, 0)
        cache.set(self.key_hour, current_hour_count + 1, timeout=60 * 60 )

        current_day_count = cache.get(self.key_day, 0)
        cache.set(self.key_day, current_day_count + 1, timeout=60 * 60 * 24 )

        ip_hour_count = cache.get(ip_key_hour, 0)
        cache.set(ip_key_hour, ip_hour_count + 1, timeout=60 * 60)

        for user in self.get_users(email):
            context = {
                'email': user.email,
                'domain': domain_override or request.get_host(),
                'site_name': 'TravelVerse',
                'uid': urlsafe_base64_encode(force_bytes(user.pk)),
                'user': user,
                'username': getattr(user, 'name', user.get_username()),
                'token': token_generator.make_token(user) if token_generator else '',
                'protocol': 'https' if use_https else 'http',
                **(extra_email_context or {})
            }

            subject = render_to_string(subject_template_name, context).strip()
            body = render_to_string(email_template_name, context)

            print("SENDING EMAIL TO:", user.email) 

            self.send_custom_mail(subject, body, user.email)

    def send_custom_mail(self, subject, body, recipient):
        connection = get_email_connection()
        send_mail(
            subject,
            body,
            settings.DEFAULT_FROM_EMAIL,
            [recipient],
            connection=connection
        )

    def _get_ip(self, request):
        """Returns the client's IP address."""
        x_forwarded_for = request.META.get('HTTP_X_FORWARDED_FOR')
        if x_forwarded_for:
            ip = x_forwarded_for.split(',')[0]
        else:
            ip = request.META.get('REMOTE_ADDR', '')
        return ip
    


