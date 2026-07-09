from rest_framework.serializers import CharField, ModelSerializer

from apps.accounts.models import Membership
from apps.tenants.models import Clinic


class ClinicSummarySerializer(ModelSerializer):
    class Meta:
        model = Clinic
        fields = ["id", "name", "slug", "timezone"]


class MembershipSerializer(ModelSerializer):
    """Vínculo do usuário logado — alimenta o seletor de clínica do front."""

    clinic = ClinicSummarySerializer(read_only=True)
    role_display = CharField(source="get_role_display", read_only=True)

    class Meta:
        model = Membership
        fields = ["id", "clinic", "role", "role_display", "is_active"]
