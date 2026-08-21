"""
Serializers do plano plataforma (§4.8).

⚠️ Nenhum deles expõe conteúdo de clínica (RF-ADM-4/6): o que sai daqui são
contagens, estado e configuração. Paciente tem NÚMERO, nunca nome.
"""

from rest_framework.serializers import (
    CharField,
    EmailField,
    IntegerField,
    ModelSerializer,
    Serializer,
    SerializerMethodField,
    SlugField,
    ValidationError,
)

from apps.tenants.choices import SuspensionCategory
from apps.tenants.models import Clinic
from apps.tenants.platform import MAX_MOTIVO


class PlatformClinicSerializer(ModelSerializer):
    """A clínica como a plataforma a vê: configuração, estado e tamanho."""

    status_display = CharField(source="get_status_display", read_only=True)
    suspension = SerializerMethodField()
    counts = SerializerMethodField()
    channel = SerializerMethodField()

    class Meta:
        model = Clinic
        fields = [
            "id",
            "name",
            "slug",
            "status",
            "status_display",
            "timezone",
            "active_window_days",
            "ehr_provider",
            "ehr_external_tenant_id",
            "ehr_push_enabled",
            "created_at",
            "suspension",
            "counts",
            "channel",
        ]
        read_only_fields = ["id", "slug", "status", "created_at", "ehr_push_enabled"]

    def get_suspension(self, obj):
        """Nulo quando a clínica está no ar - a tela não mostra bloco vazio."""
        if not obj.is_suspended:
            return None
        return {
            "category": obj.suspension_category,
            "category_display": obj.get_suspension_category_display(),
            "reason": obj.suspension_reason,
            "since": obj.suspended_at,
        }

    def get_counts(self, obj):
        """
        O tamanho da clínica, anotado em LOTE pelo viewset.

        ⚠️ Contagens, nunca nomes. "12 pacientes" responde à pergunta do
        plano plataforma; a lista de quem são é conteúdo da clínica, e o
        admin não é membro dela (RF-ADM-6).
        """
        return {
            "members": getattr(obj, "members_count", 0),
            "patients": getattr(obj, "patients_count", 0),
            "conversations_30d": getattr(obj, "conversations_30d", 0),
            "messages_30d": getattr(obj, "messages_30d", 0),
        }

    def get_channel(self, obj):
        """Estado do WhatsApp: é o que explica clínica viva sem movimento."""
        canal = getattr(obj, "canal_ativo", None)
        if canal is None:
            return {"connected": False, "number": "", "reason": ""}
        return {
            "connected": canal.disconnected_at is None,
            "number": canal.display_number,
            "reason": canal.disconnect_reason,
        }


class PlatformClinicCreateSerializer(Serializer):
    """
    Nasce a clínica E o primeiro gestor (RF-ADM-1.2).

    O gestor não é opcional de propósito: clínica sem ele é clínica que
    ninguém acessa.
    """

    name = CharField(max_length=160)
    slug = SlugField(max_length=50)
    timezone = CharField(max_length=48, required=False, default="America/Fortaleza")
    manager_name = CharField(max_length=120)
    manager_email = EmailField()

    def validate_timezone(self, value):
        # A mesma guarda do RF-CFG-2: o fuso governa o `send_time` da
        # sequência, o `TruncDate` da agenda e o horário de funcionamento. Um
        # valor que o `ZoneInfo` não conhece quebraria os três longe daqui.
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValidationError("Fuso horário desconhecido.") from exc
        return value


class PlatformClinicUpdateSerializer(ModelSerializer):
    """
    O que a plataforma edita (RF-ADM-1.3).

    ⚠️ `ehr_credentials` NÃO está aqui e não pode entrar: chave de integração
    numa tela web é superfície nova para o que hoje está cifrado em repouso.
    O `slug` também fica fora - ele é endereço, e mudá-lo quebra comando e URL.
    """

    class Meta:
        model = Clinic
        fields = [
            "name",
            "timezone",
            "active_window_days",
            "ehr_provider",
            "ehr_external_tenant_id",
        ]

    def validate_timezone(self, value):
        # A mesma guarda do RF-CFG-2: o fuso governa o `send_time` da
        # sequência, o `TruncDate` da agenda e o horário de funcionamento. Um
        # valor que o `ZoneInfo` não conhece quebraria os três longe daqui.
        from zoneinfo import ZoneInfo

        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValidationError("Fuso horário desconhecido.") from exc
        return value


class ClinicSuspendSerializer(Serializer):
    """Suspender EXIGE dizer por quê (RF-ADM-1.4)."""

    category = CharField()
    reason = CharField(max_length=MAX_MOTIVO)

    def validate_category(self, value):
        if value not in SuspensionCategory.values:
            raise ValidationError("Escolha o motivo da suspensão.")
        return value


class PlatformOverviewSerializer(Serializer):
    """Os números do topo (RF-ADM-4). Só leitura, montado no viewset."""

    clinics = IntegerField()
    clinics_active = IntegerField()
    clinics_suspended = IntegerField()
    users = IntegerField()
    patients = IntegerField()
    conversations_30d = IntegerField()
    messages_30d = IntegerField()


__all__ = [
    "ClinicSuspendSerializer",
    "PlatformClinicCreateSerializer",
    "PlatformClinicSerializer",
    "PlatformClinicUpdateSerializer",
    "PlatformOverviewSerializer",
]
