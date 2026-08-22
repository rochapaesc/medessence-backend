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
    last_message_at = SerializerMethodField()

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
            "last_message_at",
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

    def get_last_message_at(self, obj):
        """O "está viva?" da lista (RF-ADM-4.3), anotado em lote pelo viewset."""
        return getattr(obj, "last_message_at", None)

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


class PlatformClinicDetailSerializer(PlatformClinicSerializer):
    """
    O detalhe em seis cartões (RF-ADM-1.8).

    Consultas POR OBJETO de propósito: o retrieve é uma clínica só, e puxar
    isto na LISTA multiplicaria o custo por tenant. A cerca continua a mesma:
    equipe é cadastro (nome, papel, último acesso); paciente, conversa e
    prontuário não atravessam.
    """

    channel_details = SerializerMethodField()
    sync_runs = SerializerMethodField()
    automation = SerializerMethodField()
    team = SerializerMethodField()
    suspension_history = SerializerMethodField()

    class Meta(PlatformClinicSerializer.Meta):
        fields = PlatformClinicSerializer.Meta.fields + [
            "channel_details",
            "sync_runs",
            "automation",
            "team",
            "suspension_history",
        ]

    def get_channel_details(self, obj):
        """O mesmo desenho que a Administração da clínica ganhou (RF-CFG-4.1)."""
        from apps.inbox.models import Channel, WhatsAppTemplate

        canal = (
            Channel.objects.filter(clinic=obj, is_test=False, deleted_at__isnull=True)
            .order_by("-connected_at", "-id")
            .first()
        )
        if canal is None:
            return None
        templates = WhatsAppTemplate.objects.filter(clinic=obj, deleted_at__isnull=True)
        return {
            "number": canal.display_number,
            "verified_name": canal.verified_name,
            "connected_at": canal.connected_at,
            "is_coexistence": canal.is_coexistence,
            "connection_source": canal.connection_source,
            "disconnected": canal.disconnected,
            "disconnected_at": canal.disconnected_at,
            "disconnect_reason": canal.disconnect_reason,
            "templates_approved": templates.filter(status__iexact="APPROVED").count(),
            "templates_pending": templates.filter(status__iexact="PENDING").count(),
        }

    def get_sync_runs(self, obj):
        """A última execução por tipo, na MESMA serialização do GET /sync/ehr/."""
        from apps.integrations.api.views import STATUS_KINDS, _serialize
        from apps.integrations.models import SyncRun

        if not obj.ehr_provider:
            return []
        runs = []
        for kind in STATUS_KINDS:
            run = (
                SyncRun.objects.filter(clinic=obj, kind=kind)
                .order_by("-started_at")
                .first()
            )
            runs.append(_serialize(kind, run))
        return runs

    def get_automation(self, obj):
        from apps.automation.choices import FlowStatus
        from apps.automation.models import Flow, Sequence
        from apps.automation.models.sequence import SequenceDispatch

        desde = _desde_30d()
        return {
            "active_flows": Flow.objects.filter(
                clinic=obj, status=FlowStatus.ACTIVE, deleted_at__isnull=True
            ).count(),
            "active_sequences": Sequence.objects.filter(
                clinic=obj, is_active=True, deleted_at__isnull=True
            ).count(),
            "dispatches_30d": SequenceDispatch.objects.filter(
                enrollment__clinic=obj, created_at__gte=desde
            ).count(),
        }

    def get_team(self, obj):
        """Nome, papel e último acesso: cadastro, não conteúdo (RF-ADM-1.8e)."""
        from apps.accounts.models.membership import Membership

        vinculos = (
            Membership.objects.filter(clinic=obj, deleted_at__isnull=True)
            .select_related("user")
            .order_by("role", "user__first_name")
        )
        return [
            {
                "name": m.user.get_full_name() or m.user.email,
                "email": m.user.email,
                "role": m.role,
                "role_display": m.get_role_display(),
                "is_active": m.is_active and m.user.is_active,
                "last_login": m.user.last_login,
            }
            for m in vinculos
        ]

    def get_suspension_history(self, obj):
        """A auditoria É o histórico (RF-ADM-1.5): nenhuma tabela nova."""
        from apps.core.models.audit_log import AuditLog

        eventos = (
            AuditLog.objects.filter(
                resource="Clinic",
                resource_id=str(obj.pk),
                payload__operation__in=["clinic.suspend", "clinic.reactivate"],
            )
            .select_related("user")
            .order_by("-timestamp")[:10]
        )
        return [
            {
                "operation": e.payload.get("operation", ""),
                "category": e.payload.get("category", ""),
                "reason": e.payload.get("reason", ""),
                "at": e.timestamp,
                "actor": (e.user.get_full_name() or e.user.email) if e.user else "",
            }
            for e in eventos
        ]


def _desde_30d():
    from datetime import timedelta

    from django.utils import timezone

    return timezone.now() - timedelta(days=30)
