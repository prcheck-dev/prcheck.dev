from django.urls import path

from . import views

app_name = "reviews"

urlpatterns = [
    path("", views.reviews, name="reviews"),
    path("<int:pk>/", views.review_detail, name="review-detail"),
    path("webhook/github/", views.github_webhook, name="github-webhook"),
]
