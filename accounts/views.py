from django.shortcuts import render, redirect
from django.contrib import messages
from .models import *
from django.contrib.auth import login, logout
from django.contrib.auth.decorators import login_required
import re
from django.contrib.auth.views import PasswordResetView
from .forms import RateLimitedPasswordResetForm
from django.urls import reverse_lazy



def login_page(request):  
    if request.method == "GET":
        if request.user.is_authenticated:
            messages.info(request, "Already Logged in")
            return redirect("meetings:dashboard")
        else:
            next_url = request.GET.get('next', '') or request.session.get('next_url', '')
            prefill = request.session.pop('loginprefill', None)
            request.session['next_url'] = next_url
            context = {
                            'next_url': next_url,
                            'prefill':prefill,
            }
            return render(request, 'accounts/login_page.html', context)
        
    elif request.method == "POST":
        email = request.POST.get('email')
        password = request.POST.get('password')
        remember_me = request.POST.get("remember_me")
        next_url = request.POST.get('next')

        user = User.objects.filter(email=email).first()
            
        if not user:
            request.session['next_url'] = next_url
            prefill = {
                'email': email,
                'password': "",
            }
            request.session['loginprefill'] = prefill
            messages.error(request, "Invalid Username.")
            return redirect('accounts:login_page')
        

        # Check password
        elif not user.check_password(password):
            request.session['next_url'] = next_url
            prefill = {
                'email': email,
                'password': "",
            }
            request.session['loginprefill'] = prefill
            messages.error(request, "Invalid Password.")
            return redirect('accounts:login_page')

        # Check if user is active
        elif not user.is_active:
            request.session['next_url'] = next_url
            prefill = {
                'email': email,
                'password': "",
            }
            request.session['loginprefill'] = prefill
            messages.error(request, "Account inactive. Contact Admin.")
            return redirect('accounts:login_page')
        
        else:
            # Log in the user
            login(request, user)
            request.session.pop('next_url', None)
            messages.success(request, "Login Successfully.")
            if remember_me == "on":
                request.session.set_expiry(6 * 60 * 60)
                return redirect(next_url or 'meetings:dashboard')
            else:
                request.session.set_expiry(0)
                return redirect(next_url or 'meetings:dashboard')
            
    


def signup_page(request):
    if request.method == "GET":
        if request.user.is_authenticated:
            messages.info(request, "Already Logged in")
            return redirect("meetings:dashboard")
        
        else:
            next_url = request.session.get('next_url', '')
            prefill = request.session.pop('signupprefill', None)
            context = {
                'prefill':prefill,
            }
            return render(request,'accounts/signup_page.html', context)
        
    elif request.method == "POST":
        name = request.POST.get('name')
        email = request.POST.get('email')
        password = request.POST.get('password')
        confirm_password = request.POST.get('confirm_password')
        next_url = request.session.get('next_url', '')
        
        valid = True

        if not name.strip():
            messages.error(request, "Name cannot be empty or spaces only.")
            valid = False
        elif len(name) >= 30 :
            messages.error(request, "Name should contain less than 30 letters.")
            valid = False
        elif not re.fullmatch(r'[A-Za-z ]+', name):
            messages.error(request, "Name can only contain letters and spaces.")
            valid = False
        elif len(re.sub(r'[^A-Za-z]', '', name)) < 4:
            messages.error(request, "Name must contain at least 4 letters.")
            valid = False

        # 2. Email validation
        if not re.fullmatch(r'^[A-Za-z0-9._%+-]+@[A-Za-z0-9.-]+\.[A-Za-z]{2,}$', email):
            messages.error(request, "Email ID invalid")
            valid = False
        elif User.objects.filter(email=email).exists():
            messages.error(request, "Email ID already registered.")
            valid = False


        # 3. Password validation
        if len(password) < 8:
            messages.error(request, "Password should be more than 8 characters.")
            valid = False
        if len(password) > 16:
            messages.error(request, "Password should be less than 16 characters.")
            valid = False
        elif not re.search(r'[!@#$%^&*(),.?":{}|<>]', password):
            messages.error(request, "Password must include at least one special character.")
            valid = False
        elif not re.search(r'[A-Z]', password):
            messages.error(request, "Password must include at least one uppercase letter.")
            valid = False
        elif not re.search(r'[a-z]', password):
            messages.error(request, "Password must include at least one lowercase letter.")
            valid = False
        elif not re.search(r'\d', password):
            messages.error(request, "Password must include at least 1 number.")
            valid = False

        # 4. Confirm password match
        if password != confirm_password:
            messages.error(request, "Confirm Passwords not match with password.")
            valid = False

        if valid:
            # Create user
            user = User.objects.create_user(
                name=name,
                email=email,
                password=password,
            )

            login(request, user)
            request.session.pop('next_url', None)
            messages.success(request, "Signed Up Successfully.")
            return redirect(next_url or 'meetings:dashboard')
            
        else:
            prefill = {
                'name': name,
                'email': email,
                "password": "",
                "confirm_password" : "",
            }
            request.session['signupprefill'] = prefill
            request.session['next_url'] = next_url
            return redirect('accounts:signup_page')
     
     
        
@login_required
def user_logout(request):
    logout(request)  
    messages.success(request, "Logged out successfully.")
    return redirect('meetings:dashboard')



class CustomPasswordResetView(PasswordResetView):

    form_class = RateLimitedPasswordResetForm
    template_name = 'accounts/password_reset_form.html'
    email_template_name = 'accounts/password_reset_email.html'
    subject_template_name = 'accounts/password_reset_subject.txt'

    success_url = reverse_lazy('accounts:password_reset_done')

    def get_form_kwargs(self):
        kwargs = super().get_form_kwargs()
        kwargs['request'] = self.request  # Pass request to form for IP access
        return kwargs