from django.urls import path

from . import views

app_name = "accounts"

urlpatterns = [
    path("github/login/", views.github_login, name="github-login"),
    path("github/callback/", views.github_callback, name="github-callback"),
    path("token/refresh/", views.token_refresh, name="token-refresh"),
    path("logout/", views.logout, name="logout"),
    path("me/", views.me, name="me"),
]
