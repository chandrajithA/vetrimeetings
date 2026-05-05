"""
management/commands/create_subscription_plans.py

Seeds the three default subscription plans.
Run once after migration:
    python manage.py create_subscription_plans
"""

from django.core.management.base import BaseCommand
from meetings.models import SubscriptionPlan   # adjust app label if needed


PLANS = [
    {
        "name":                  "free",
        "display_name":          "Free",
        "description":           "Perfect for personal use and quick catch-ups.",
        "max_duration_minutes":  60,   # 1 hours
        "max_participants":      20,
        "can_record":            False,
        "can_use_waiting_room":  False,
        "can_schedule":          False,
        "price_monthly":         0.00,
    },
    {
        "name":                  "basic",
        "display_name":          "Basic",
        "description":           "For small teams — longer meetings and more guests.",
        "max_duration_minutes":  1440,   # 5 hours
        "max_participants":      100,
        "can_record":            True,
        "can_use_waiting_room":  True,
        "can_schedule":          True,
        "price_monthly":         1499.99,
    },
    {
        "name":                  "premium",
        "display_name":          "Premium",
        "description":           "Enterprise-grade — unlimited time, max guests, all features.",
        "max_duration_minutes":  0,      # 0 for unlimited
        "max_participants":      300,
        "can_record":            True,
        "can_use_waiting_room":  True,
        "can_schedule":          True,
        "price_monthly":         2999.99,
    },
]


class Command(BaseCommand):
    help = "Seed the default Free / Basic / Premium subscription plans."

    def handle(self, *args, **options):
        for data in PLANS:
            plan, created = SubscriptionPlan.objects.update_or_create(
                name=data["name"],
                defaults=data,
            )
            verb = "Created" if created else "Updated"
            self.stdout.write(self.style.SUCCESS(f"{verb}: {plan}"))

        self.stdout.write(self.style.SUCCESS("\nDone. Plans are ready."))
