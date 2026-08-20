"""Equipe da clínica (§4.12, RF-EQP)."""

from rest_framework.serializers import (
    BooleanField,
    CharField,
    ChoiceField,
    EmailField,
    IntegerField,
    ModelSerializer,
    PrimaryKeyRelatedField,
    Serializer,
    SerializerMethodField,
)

from apps.accounts.api.serializers.membership import PractitionerSummarySerializer
from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership
from apps.scheduling.models import Practitioner


class TeamMemberSerializer(ModelSerializer):
    """A linha da lista de equipe."""

    user_id = IntegerField(source="user.pk", read_only=True)
    name = CharField(source="user.get_full_name", read_only=True)
    email = EmailField(source="user.email", read_only=True)
    role_display = CharField(source="get_role_display", read_only=True)
    practitioner = PractitionerSummarySerializer(read_only=True)
    # Quem ainda não entrou pela primeira vez (o selo "Senha temporária").
    must_change_password = BooleanField(source="user.must_change_password", read_only=True)
    # Quantas conversas voltam para a fila se esta pessoa for desativada
    # (RF-EQP-5.1): o diálogo de confirmação promete o número.
    open_conversations = IntegerField(read_only=True, default=0)
    is_me = SerializerMethodField()

    class Meta:
        model = Membership
        fields = [
            "id",
            "user_id",
            "name",
            "email",
            "role",
            "role_display",
            "practitioner",
            "is_active",
            "must_change_password",
            "open_conversations",
            "is_me",
        ]

    def get_is_me(self, obj) -> bool:
        """A tela não oferece à pessoa as ações que o servidor vai recusar."""
        request = self.context.get("request")
        return bool(request and obj.user_id == request.user.pk)


class TeamMemberCreateSerializer(Serializer):
    email = EmailField()
    name = CharField(required=False, allow_blank=True)
    role = ChoiceField(choices=MembershipRole.choices)
    practitioner = PrimaryKeyRelatedField(
        queryset=Practitioner.objects.all(), required=False, allow_null=True
    )


class TeamMemberUpdateSerializer(Serializer):
    """
    O e-mail não está aqui de propósito (RF-EQP-4): é a identidade de acesso, e
    quem edita o login de outra pessoa toma a conta dela.
    """

    name = CharField(required=False, allow_blank=True)
    role = ChoiceField(choices=MembershipRole.choices, required=False)
    practitioner = PrimaryKeyRelatedField(
        queryset=Practitioner.objects.all(), required=False, allow_null=True
    )
