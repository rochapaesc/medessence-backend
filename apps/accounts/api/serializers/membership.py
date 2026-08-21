from rest_framework.serializers import CharField, ModelSerializer, SerializerMethodField

from apps.accounts.models import Membership
from apps.scheduling.models import Practitioner
from apps.tenants.models import Clinic


class ClinicSummarySerializer(ModelSerializer):
    # Trava temporária de dado de produção: o front esconde o que exclui
    # quando ela está ligada, para o botão não prometer o que a API recusa.
    data_guard = SerializerMethodField()

    class Meta:
        model = Clinic
        # ⚠️ `status` viaja aqui porque a tela precisa saber da suspensão ANTES
        # de pedir qualquer coisa (RF-ADM-1.7e). Esperar o 403 mostraria a tela
        # errada primeiro, e é a mesma razão de `must_change_password` estar no
        # `/me/`. O MOTIVO não vem junto: ele é para a plataforma responder.
        fields = ["id", "name", "slug", "timezone", "status", "data_guard"]

    def get_data_guard(self, obj) -> bool:
        from apps.core.api.guards import guard_is_on

        return guard_is_on(obj)


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
