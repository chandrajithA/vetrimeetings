from django.core.mail import EmailMultiAlternatives
from django.template.loader import render_to_string
from django.conf import settings


def send_meeting_invite(meeting, invitee_emails):
    """Send a meeting invite email to a list of email addresses."""
    subject = f"Vetri Meeting Invite: {meeting.title}"

    for email in invitee_emails:
        context = {
            'meeting': meeting,
            'join_url': meeting.meeting_url,
        }
        html_body = render_to_string('meetings/email/invite.html', context)
        text_body = render_to_string('meetings/email/invite.txt',  context)

        msg = EmailMultiAlternatives(
            subject=subject,
            body=text_body,
            from_email=settings.DEFAULT_FROM_EMAIL,
            to=[email],
        )
        msg.attach_alternative(html_body, "text/html")
        msg.send(fail_silently=True)