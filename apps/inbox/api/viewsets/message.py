from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.mixins import (
    CreateModelMixin,
    DestroyModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.response import Response
from rest_framework.status import HTTP_201_CREATED

from apps.core.api.viewsets import ClinicScopedMixin
from apps.core.api.viewsets.base import BaseGenericViewSet
from apps.core.audit import log_action
from apps.core.mixins import AuditMixin
from apps.core.models.audit_log import AuditAction
from apps.inbox.api.filtersets import MessageFilterset
from apps.inbox.api.serializers import (
    MessageCreateSerializer,
    MessageEditSerializer,
    MessageSerializer,
)
from apps.inbox.api.serializers.message import media_payload
from apps.inbox.choices import MessageStatus, SenderKind
from apps.inbox.models import Message


class MessageViewSet(
    AuditMixin,
    ClinicScopedMixin,
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
    DestroyModelMixin,
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
        "update": MessageEditSerializer,
        "partial_update": MessageEditSerializer,
    }

    def get_queryset(self):
        """
        Do MAIS NOVO para o mais antigo — e é o cliente que inverte para
        desenhar.

        Parece ao contrário, mas é o que faz a conversa longa funcionar: a
        página 1 tem de ser o FIM do papo, que é onde o atendimento continua.
        Em ordem crescente, a primeira página de uma conversa de 800 mensagens
        traria as 30 mais ANTIGAS — quem abrisse cairia no "oi" de seis meses
        atrás, e as de hoje só apareceriam depois de 26 páginas.

        `-id` desempata: mensagens do mesmo segundo (rajada de mídia, echo do
        celular) sairiam em ordem instável entre uma página e outra, e a
        paginação repetiria ou pularia balões.
        """
        # Os selos vêm junto: sem o prefetch, uma thread de 30 balões faria
        # 30 consultas só para desenhar as reações.
        return (
            super()
            .get_queryset()
            .prefetch_related("reactions__actor_user")
            .order_by("-wa_timestamp", "-id")
        )

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

    @action(detail=True, methods=["post"], url_path="react")
    def react(self, request, pk=None):
        """
        Cola (ou tira, com emoji vazio) o selo da EQUIPE numa mensagem.

        A tabela já guardava reação de atendente desde a fatia 4 — só não havia
        por onde criar uma. Sem isto a recepção recebia 👍 do paciente e não
        tinha como devolver, o que no WhatsApp é a resposta mais barata que
        existe para "recebi, obrigada".
        """
        from apps.inbox.services import reagir

        message = self.get_object()
        self._assert_can_write(message.conversation)
        emoji = (request.data.get("emoji") or "").strip()
        if len(emoji) > 16:
            raise ValidationError("Reação inválida.")
        if not message.provider_message_id:
            raise ValidationError(
                "Esta mensagem ainda não chegou ao WhatsApp. Não dá para reagir."
            )

        reagir(message, request.user, emoji)

        message.refresh_from_db()
        serializer = MessageSerializer(message, context=self.get_serializer_context())
        return Response(serializer.data)

    @action(detail=True, methods=["post"], url_path="resend")
    def resend(self, request, pk=None):
        """
        Reenvia UMA mensagem que falhou (o botão no balão vermelho).

        É o padrão do Chatwoot (`MessageError.vue`): quem decide reenviar é
        quem atende. Uma mensagem presa meia hora pode ter virado assunto
        vencido — reenviar sozinho ao reconectar mandaria para o paciente algo
        que não faz mais sentido.
        """
        from apps.inbox.services import reenviar

        message = self.get_object()
        self._assert_can_write(message.conversation)
        self._assert_canal_no_ar()

        if message.status != MessageStatus.FAILED or message.is_internal:
            raise ValidationError("Esta mensagem não está aguardando reenvio.")
        if not reenviar(message):
            raise ValidationError(
                "A janela de 24h desta conversa fechou. Agora só por template aprovado."
            )

        message.refresh_from_db()
        serializer = MessageSerializer(message, context=self.get_serializer_context())
        return Response(serializer.data)

    @action(detail=False, methods=["post"], url_path="resend-failed")
    def resend_failed(self, request):
        """
        Reenvia tudo o que ficou preso na queda (o botão da faixa verde).

        Devolve o que aconteceu com cada grupo em vez de um "pronto" mudo: se
        3 falharam e só 2 saem, a recepção tem de saber que 1 continua parada
        — faixa que some calada é como se perde a confiança nela.
        """
        from apps.inbox.services import mensagens_para_reenviar, reenviar

        self._assert_canal_no_ar()

        reenviadas, fora_da_janela = 0, 0
        # Da mais ANTIGA para a mais nova: cada reenvio carimba o horário de
        # agora, então processar fora de ordem inverteria a conversa — o
        # paciente receberia a resposta antes da pergunta.
        presas = (
            mensagens_para_reenviar(self.clinic)
            .select_related("conversation")
            .order_by("wa_timestamp", "id")
        )
        for message in presas:
            if reenviar(message):
                reenviadas += 1
            else:
                fora_da_janela += 1
        return Response({"reenviadas": reenviadas, "fora_da_janela": fora_da_janela})

    def _assert_canal_no_ar(self):
        """
        Reenviar para um canal morto só produz a mesma falha, agora com a
        recepção achando que resolveu.
        """
        from apps.inbox.services import canal_da_clinica

        canal = canal_da_clinica(self.clinic)
        if canal is not None and canal.disconnected:
            raise ValidationError(
                "O WhatsApp ainda está desconectado. Reenvie quando a conexão voltar."
            )

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

    # ------------------------- editar e excluir ------------------------- #

    def perform_update(self, serializer):
        """
        Reescreve a NOTA INTERNA — e só ela.

        Mensagem enviada fica intocada, e não é limitação nossa: a Cloud API
        não edita mensagem entregue. O paciente já leu o original; mudar o
        texto aqui faria a thread contar uma história que o celular dele
        desmente. Nenhum dos três repositórios de referência edita mensagem.
        """
        self._assert_pode_mexer(serializer.instance, acao="editar")
        if not serializer.instance.is_internal:
            raise ValidationError(
                "Só nota da equipe pode ser editada. Mensagem já enviada o "
                "paciente recebeu — para corrigir, mande outra."
            )
        super().perform_update(serializer)  # AuditMixin guarda o antes/depois
        serializer.instance.edited_at = timezone.now()
        serializer.instance.save(update_fields=["edited_at", "updated_at"])

        from apps.inbox.realtime import notify_conversation_updated_on_commit

        notify_conversation_updated_on_commit(serializer.instance.conversation)

    def perform_destroy(self, instance):
        """
        Apaga o que NUNCA CHEGOU ao paciente: nota da equipe e mensagem que
        falhou.

        Mensagem entregue não some daqui porque não some de lá — a Cloud API
        não revoga. Deixar apagar daria a falsa sensação de que sumiu dos dois
        lados, que é pior do que não ter o botão.

        ⚠️ A mensagem NÃO some da conversa (mudou em 21/08/2026): ela fica
        ESMAECIDA, com o selo de apagada e o conteúdo legível. Antes era soft
        delete e sumia da thread, o que apagava da vista da equipe um pedaço
        do atendimento que existiu — o registro é da clínica, e quem leu
        aquilo respondeu com base naquilo. É a mesma regra do apagar que vem
        do WhatsApp (RF-INB-6.6), agora valendo para os três caminhos: CRM,
        app do celular e paciente.

        `revoked_by` guarda QUEM apagou aqui; a auditoria registra o DELETE.
        """
        self._assert_pode_mexer(instance, acao="excluir")
        entregue = bool(instance.provider_message_id) and not instance.is_internal
        if entregue:
            raise ValidationError(
                "Esta mensagem já foi entregue ao paciente e não pode ser "
                "apagada — o WhatsApp dele continuaria mostrando."
            )
        if instance.revoked_at:
            raise ValidationError("Esta mensagem já foi apagada.")

        conversation = instance.conversation
        instance.revoked_at = timezone.now()
        instance.revoked_by = self.request.user
        instance.save(update_fields=["revoked_at", "revoked_by", "updated_at"])
        log_action(
            user=self.request.user,
            action=AuditAction.DELETE,
            resource="Message",
            resource_id=instance.pk,
            payload={"operation": "message.revoke", "interna": instance.is_internal},
            request=self.request,
            clinic=self.clinic,
        )

        # A prévia da fila pode ser a mensagem que acabou de sumir.
        from apps.inbox.realtime import notify_conversation_updated_on_commit
        from apps.inbox.services import recalcular_ultima_mensagem

        # A prévia passa a dizer "Mensagem apagada" no lugar do texto.
        recalcular_ultima_mensagem(conversation)
        notify_conversation_updated_on_commit(conversation)

    def _assert_pode_mexer(self, message, *, acao: str):
        """
        Mexe quem escreveu, ou o gestor.

        Nota de colega não é para outro atendente reescrever: ela é registro do
        que AQUELA pessoa soube. O gestor entra porque alguém precisa poder
        limpar o que foi escrito errado quando quem escreveu não está mais.
        """
        if message.activity_type:
            raise ValidationError("Evento da linha do tempo não pode ser alterado.")
        self._assert_can_write(message.conversation)

        from apps.accounts.choices import MembershipRole

        eh_autor = message.sent_by_id == self.request.user.pk
        eh_gestor = self.membership.role == MembershipRole.MANAGER
        if not (eh_autor or eh_gestor):
            raise PermissionDenied(
                f"Só quem escreveu (ou um gestor) pode {acao} esta mensagem."
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

        # Assina ANTES de qualquer coisa: a prévia da fila, o evento de tempo
        # real e a task de envio precisam ver o texto final. Assinar depois
        # deixaria os três com a versão sem nome.
        from apps.inbox.services import assinar_mensagem

        assinar_mensagem(message, self.request.user)

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
