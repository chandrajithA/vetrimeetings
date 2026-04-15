from django.urls import path
from .views import *
from .views import CustomPasswordResetView
from django.contrib.auth import views as auth_views

app_name = 'accounts'

urlpatterns = [
    path("signup/", signup_page, name="signup_page"),
    path("login/", login_page, name="login_page"),
    path("user_logout/", user_logout, name="user_logout"),
    path('reset_password/', CustomPasswordResetView.as_view(), name='password_reset'),
    path('reset_password_sent/', auth_views.PasswordResetDoneView.as_view(
        template_name='accounts/password_reset_done.html'
    ), name='password_reset_done'),

    path('reset/<uidb64>/<token>/', auth_views.PasswordResetConfirmView.as_view(
        template_name='accounts/password_reset_confirm.html',
        success_url=reverse_lazy('accounts:password_reset_complete') 
    ), name='password_reset_confirm'),

    path('reset_password_complete/', auth_views.PasswordResetCompleteView.as_view(
        template_name='accounts/password_reset_complete.html'
    ), name='password_reset_complete'),
]