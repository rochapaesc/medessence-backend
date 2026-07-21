from rest_framework.serializers import CharField, ModelSerializer

from apps.accounts.models import Membership
from apps.scheduling.models import Practitioner
from apps.tenants.models import Clinic


class ClinicSummarySerializer(ModelSerializer):
    class Meta:
        model = Clinic
        fields = ["id", "name", "slug", "timezone"]


class PractitionerSummarySerializer(ModelSerializer):
    class Meta:
        model = Practitioner
        fields = ["id", "name"]


class MembershipSerializer(ModelSerializer):
    """Vínculo do usuário logado - alimenta o seletor de clínica do front."""

    clinic = ClinicSummarySerializer(read_only=True)
    role_display = CharField(source="get_role_display", read_only=True)
    # `null` para papel sem carteira (gestor/atendente) e para médico ainda
    # não vinculado - o front usa isso para escopar a agenda/carteira dele.
    practitioner = PractitionerSummarySerializer(read_only=True)

    class Meta:
        model = Membership
        fields = ["id", "clinic", "role", "role_display", "is_active", "practitioner"]
