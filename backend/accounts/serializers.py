from rest_framework import serializers

from .models import User


class UserSerializer(serializers.ModelSerializer):
    class Meta:
        model = User
        fields = (
            "id",
            "email",
            "username",
            "github_id",
            "github_login",
            "avatar_url",
            "is_staff",
            "is_superuser",
            "date_joined",
            "last_login",
        )
        read_only_fields = fields
