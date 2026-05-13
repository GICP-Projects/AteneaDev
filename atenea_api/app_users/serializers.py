
from rest_framework import serializers
from django.contrib.auth.password_validation import validate_password
from app_users.models import User


class UserSerializer(serializers.ModelSerializer):
    """
    serializer to let user log in
    """
    password = serializers.CharField(
        write_only=True,
        style={'input_type': 'password'},
        required=True
    )

    class Meta:
        model = User
        fields = (
            "email", 
            "username", 
            "first_name",
            "last_name",
            "password", 
            "first_name"
        )


    def validate_password(self, value):
        """
        validate password
        """
        validate_password(value)
        return value