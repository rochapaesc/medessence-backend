from django.utils import timezone
from rest_framework.mixins import CreateModelMixin, ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.status import HTTP_201_CREATED

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
    Thread de mensagens (RF-INB-2). Somente listar/ver/criar - mensagens são
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

    def create(self, request, *args, **kwargs):
        """
        Responde com o serializer de LEITURA, não o de escrita.

        O composer precisa do recurso INTEIRO para trocar o balão otimista
        pelo definitivo: `wa_timestamp`, `direction`, `status` e
        `provider_message_id` não existem no serializer de entrada, e devolver
        só o que foi enviado deixava a tela sem como fechar o ciclo (a
        mensagem duplicava - o balão "enviando..." nunca era substituído).
        """
        write = self.get_serializer(data=request.data)
        write.is_valid(raise_exception=True)
        self.perform_create(write)

        message = write.instance
        # O envio roda em task: eager nos testes já gravou wamid/status, e em
        # produção o refresh custa uma query e evita devolver dado velho.
        message.refresh_from_db()
        read = MessageSerializer(message, context=self.get_serializer_context())
        return Response(
            read.data,
            status=HTTP_201_CREATED,
            headers=self.get_success_headers(read.data),
        )

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
