from django.contrib import admin
from .models import User, UserLoginActivity


class CustomUserAdmin(admin.ModelAdmin):
    model = User
    list_display = ('email', 'is_staff', 'is_active')
    ordering = ('email',)


admin.site.register(User, CustomUserAdmin)
admin.site.register(UserLoginActivity)