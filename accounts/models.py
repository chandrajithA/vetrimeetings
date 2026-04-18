from django.contrib.auth.models import AbstractBaseUser, BaseUserManager, PermissionsMixin
from django.db import models
from django.conf import settings
import uuid
from django.utils.text import slugify
from django.templatetags.static import static


def profile_image_upload_path(instance, filename):
    ext = filename.split('.')[-1]
    new_filename = f"{uuid.uuid4()}.{ext}"

    name = slugify(instance.name or "user")
    user_id = instance.pk or "temp"

    return f'User_images/{name}_ID_{user_id}/{new_filename}'

# Create your models here.
class CustomUserManager(BaseUserManager):
    def create_user(self, name, email, password=None, **extra_fields):
        if not name:
            raise ValueError('Name is required')
        elif not email:
            raise ValueError('Email is required')
        elif not password:
            raise ValueError('Password is required')
        if email:
            email = self.normalize_email(email)
        user = self.model(name=name, email=email, **extra_fields)
        user.set_password(password)
        user.save(using=self._db)
        return user

    def create_superuser(self, name, email, password=None, **extra_fields):
        extra_fields.setdefault('is_staff', True)
        extra_fields.setdefault('is_superuser', True)
        return self.create_user(name, email, password, **extra_fields)
    

class User(AbstractBaseUser, PermissionsMixin):

    user_profile_picture = models.ImageField( null=True, blank=True, upload_to=profile_image_upload_path)
    name = models.CharField(max_length=50, null=False, blank=False)
    email = models.EmailField(unique=True, null=False, blank=False)

    # Permissions / status flags
    is_active = models.BooleanField(null=True, blank=True, default=True)
    is_staff = models.BooleanField(null=True, blank=True, default=False)
    is_superuser = models.BooleanField(null=True, blank=True, default=False)

    # Timestamps
    created_at = models.DateTimeField(auto_now_add=True)

    objects = CustomUserManager()

    USERNAME_FIELD = 'email'
    REQUIRED_FIELDS = ['name']

    def __str__(self):
        return self.email
        
    @property
    def profile_picture_url(self):
        if self.user_profile_picture and self.user_profile_picture.name:
            return self.user_profile_picture.url
        return static('images/user_image_default.png')


class UserLoginActivity(models.Model):
    user = models.ForeignKey(
        settings.AUTH_USER_MODEL,
        on_delete=models.CASCADE,
        related_name='logindetail'
    )
    
    ip_address = models.GenericIPAddressField(null=True, blank=True)
    login_at = models.DateTimeField(auto_now_add=True)
    logout_at = models.DateTimeField(auto_now_add=True)

    class Meta:
        indexes = [
            models.Index(fields=['login_at']),
        ]

    def __str__(self):
        return f"{self.user.name} - {self.login_at}"