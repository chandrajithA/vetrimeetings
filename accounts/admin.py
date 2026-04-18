from django.contrib import admin
from django.contrib.auth.admin import UserAdmin
from .models import User, UserLoginActivity


@admin.register(User)
class CustomUserAdmin(UserAdmin):
    model = User

    # ✅ Correct fields
    list_display = ("email", "name", "is_staff", "is_superuser", "is_active")
    list_filter = ("is_staff", "is_superuser", "is_active")

    fieldsets = (
        (None, {"fields": ("email", "password")}),
        ("Personal info", {
            "fields": ("name", "user_profile_picture")
        }),
        ("Permissions", {
            "fields": ("is_active", "is_staff", "is_superuser", "groups", "user_permissions")
        }),
        ("Important dates", {"fields": ("last_login",)}),
    )

    add_fieldsets = (
        (None, {
            "classes": ("wide",),
            "fields": ("email", "name", "password1", "password2", "is_staff", "is_superuser"),
        }),
    )

    search_fields = ("email", "name")
    ordering = ("email",)

    def has_add_permission(self, request):
        return False


@admin.register(UserLoginActivity)
class UserLoginActivityAdmin(admin.ModelAdmin):
    list_display = (
        "user",
        "login_at",
        "ip_address",
    )

    list_filter = (
        "login_at",
    )

    search_fields = (
        "user__username",
        "user__email",
    )

    ordering = ("-login_at",)

    readonly_fields = (
        "user",
        "login_at",
    )

    list_per_page = 50