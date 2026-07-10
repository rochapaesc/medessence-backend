from apps.core.api.viewsets import (
    ClinicScopedModelViewSet,
    ClinicScopedReadOnlyViewSet,
)
from apps.core.mixins import AuditMixin
from apps.inbox.api.serializers import (
    QuickReplySerializer,
    WhatsAppTemplateSerializer,
)
from apps.inbox.models import QuickReply, WhatsAppTemplate


class QuickReplyViewSet(AuditMixin, ClinicScopedModelViewSet):
    """CRUD de respostas rápidas (RF-INB-8)."""

    model = QuickReply
    audit_resource = "QuickReply"
    serializer_class = QuickReplySerializer
    ordering_fields = ["label"]

    def get_queryset(self):
        return super().get_queryset().order_by("label")


class WhatsAppTemplateViewSet(ClinicScopedReadOnlyViewSet):
    """Templates aprovados (RF-INB-3) — read-only; populados pelo beat na Fatia B."""

    model = WhatsAppTemplate
    serializer_class = WhatsAppTemplateSerializer
    ordering_fields = ["name"]

    def get_queryset(self):
        return super().get_queryset().order_by("name")
