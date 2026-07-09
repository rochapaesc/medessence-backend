from rest_framework.serializers import CharField, ModelSerializer

from apps.accounts.models import User


class UserMeSerializer(ModelSerializer):
    full_name = CharField(source="get_full_name", read_only=True)

    class Meta:
        model = User
        fields = [
            "id",
            "email",
            "first_name",
            "last_name",
            "full_name",
            "is_platform_admin",
        ]
        read_only_fields = ["id", "email", "is_platform_admin"]


class UserMeUpdateSerializer(ModelSerializer):
    class Meta:
        model = User
        fields = ["first_name", "last_name"]
