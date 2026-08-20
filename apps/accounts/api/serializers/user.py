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
            # A tela precisa saber que a senha é temporária para levar à troca
            # (RF-EQP-7): o 403 do gate só chega quando ela tenta ir a outro
            # lugar, e aí a pessoa já viu a tela errada.
            "must_change_password",
        ]
        read_only_fields = ["id", "email", "is_platform_admin", "must_change_password"]


class UserMeUpdateSerializer(ModelSerializer):
    class Meta:
        model = User
        fields = ["first_name", "last_name"]
