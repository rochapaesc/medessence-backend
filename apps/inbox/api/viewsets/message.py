from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.mixins import CreateModelMixin, ListModelMixin, RetrieveModelMixin
from rest_framework.response import Response
from rest_framework.status import HTTP_201_CREATED

from apps.core.api.viewsets import ClinicScopedMixin
from apps.core.api.viewsets.base import BaseGenericViewSet
from apps.core.mixins import AuditMixin
from apps.inbox.api.filtersets import MessageFilterset
from apps.inbox.api.serializers import MessageCreateSerializer, MessageSerializer
from apps.inbox.api.serializers.message import media_payload
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

    @action(detail=True, methods=["post"], url_path="retry-media")
    def retry_media(self, request, pk=None):
        """
        Reenfileira o download da mídia (o "Tentar de novo" da bolha).

        Vale a pena tentar de novo porque a maioria das falhas é passageira:
        rede caindo no meio, token expirado que já foi trocado, worker que
        estava fora do ar. O que não volta é mídia velha demais — a URL da
        Meta expira, e aí a falha se repete com o mesmo motivo, agora escrito
        na tela em vez de silenciosa.
        """
        from apps.inbox.choices import MediaState
        from apps.inbox.tasks import fetch_media_asset

        message = self.get_object()
        if not message.media_id:
            raise ValidationError("Esta mensagem não tem mídia.")

        media = message.media
        if media.stored_file:
            # Já está no disco: nada a refazer, e a tela só precisa se atualizar.
            return Response(media_payload(media, request))

        media.state = MediaState.PENDING
        media.error = ""
        media.save(update_fields=["state", "error"])
        fetch_media_asset.delay(media.pk)
        return Response(media_payload(media, request))

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

        # Trava de posse (RF-ATD-14/15): a barreira vive no SERVIDOR. Front
        # desabilitando o campo protege do descuido; não protege de duas abas,
        # de um F5 no meio, nem da IA — que não passa pela tela.
        self._assert_can_write(write.validated_data["conversation"])

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

    def _assert_can_write(self, conversation):
        """
        Recusa quem não tem a caneta, com erro que a tela sabe traduzir em
        "Ana assumiu esta conversa" — nunca um vermelho genérico (RF-ATD-15.3).
        """
        from apps.inbox.attendance import ConversationBusy, assert_can_write

        try:
            assert_can_write(conversation, self.request.user)
        except ConversationBusy as exc:
            raise PermissionDenied(
                {
                    "detail": "Esta conversa está sendo atendida por outra pessoa.",
                    "code": "conversation_busy",
                    "attended_by": exc.attended_by,
                    "holder": exc.holder,
                }
            ) from exc

    def perform_create(self, serializer):
        super().perform_create(serializer)  # AuditMixin → ClinicScoped → save
        message = serializer.instance

        # Escrever em conversa livre é o ato que a assume (RF-ATD-14) — senão
        # a recepção daria dois cliques para responder a primeira do dia.
        # Nota interna NÃO assume: anotar não é atender.
        if not message.is_internal:
            self._claim_if_free(message.conversation)

        # A fila de TODO MUNDO reordena e ganha a prévia nova quando o
        # atendente escreve - antes, só o inbound emitia conversation:updated
        # e a mensagem enviada não subia a conversa na lista de ninguém.
        # Vale também para a nota interna: ela vira prévia.
        from apps.inbox.realtime import notify_conversation_updated_on_commit

        notify_conversation_updated_on_commit(message.conversation)

        # Nota interna nunca vai para o provedor (RF-ATD-3).
        if message.is_internal:
            return

        from apps.inbox.tasks import send_whatsapp_message

        send_whatsapp_message.delay(message.pk)

    def _claim_if_free(self, conversation):
        from apps.inbox.attendance import take_over
        from apps.inbox.choices import AttendedBy

        if conversation.attended_by == AttendedBy.NONE:
            take_over(conversation, self.request.user, expected=AttendedBy.NONE)

    def clinic_save_kwargs(self) -> dict:
        # A mensagem do composer nasce OUT/AGENT, autoria do usuário logado e
        # horário do servidor; a direção é derivada no Message.save().
        return {
            "clinic": self.clinic,
            "sender_kind": SenderKind.AGENT,
            "sent_by": self.request.user,
            "wa_timestamp": timezone.now(),
        }
