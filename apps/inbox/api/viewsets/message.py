from django.utils import timezone
from rest_framework.mixins import (
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
)

from apps.core.api.viewsets import ClinicScopedMixin
from apps.core.api.viewsets.base import BaseGenericViewSet
from apps.core.mixins import AuditMixin
from apps.inbox.api.filtersets import MessageFilterset
from apps.inbox.api.serializers import MessageCreateSerializer, MessageSerializer
from apps.inbox.choices import SenderKind
from apps.inbox.models import Message


class MessageViewSet(
    AuditMixin,
    ClinicScopedMixin,
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    BaseGenericViewSet,
):
    """
    Thread de mensagens (RF-INB-2). Somente listar/ver/criar — mensagens são
    imutáveis após criadas. Filtre por `?conversation=<id>`.

    Na Fatia A o create apenas PERSISTE a mensagem do atendente (OUT/AGENT);
    o envio real via `send_whatsapp_message` entra na Fatia B.
    """

    model = Message
    audit_resource = "Message"
    filterset_class = MessageFilterset
    serializer_class = MessageSerializer
    select_related = ["media", "sent_by"]
    ordering_fields = ["wa_timestamp"]

    action_serializer_classes = {
        "create": MessageCreateSerializer,
    }

    def get_queryset(self):
        return super().get_queryset().order_by("wa_timestamp")

    def perform_create(self, serializer):
        super().perform_create(serializer)  # AuditMixin → ClinicScoped → save
        # Fatia B: persistir e ENVIAR (provider real; FAKE devolve wamid sintético).
        from apps.inbox.tasks import send_whatsapp_message

        send_whatsapp_message.delay(serializer.instance.pk)

    def clinic_save_kwargs(self) -> dict:
        # A mensagem do composer nasce OUT/AGENT, autoria do usuário logado e
        # horário do servidor; a direção é derivada no Message.save().
        return {
            "clinic": self.clinic,
            "sender_kind": SenderKind.AGENT,
            "sent_by": self.request.user,
            "wa_timestamp": timezone.now(),
        }
