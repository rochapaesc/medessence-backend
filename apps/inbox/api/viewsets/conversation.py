from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.api.viewsets import ClinicScopedReadOnlyViewSet
from apps.core.mixins import AuditMixin
from apps.inbox.api.filtersets import ConversationFilterset
from apps.inbox.api.serializers import ConversationSerializer
from apps.inbox.choices import ConversationPriority, ConversationStatus
from apps.inbox.models import Conversation

# Teto do seletor de pessoas. Não é paginação: quem procura alguém fora do
# teto usa a busca, que é o caminho desenhado para isso. Devolver a clínica
# inteira faria uma tela de escolha carregar uma lista que ninguém lê.
AGENTS_LIMIT = 30


class ConversationViewSet(AuditMixin, ClinicScopedReadOnlyViewSet):
    """
    Lista/detalhe de conversas (RF-INB-1), ordenada por recência. As mutações
    do inbox são ações explícitas (o corpo da conversa é derivado das
    mensagens, não editável direto):

        POST /{id}/read/          zera as não lidas (RF-INB-4)
        POST /{id}/assign/        assume o atendimento (RF-INB-5/8)
        POST /{id}/mark-waiting/  marca como aguardando atendente (manual - F2)
        POST /{id}/link-patient/  desambigua o vínculo contato↔paciente (RF-INB-7)
        POST /{id}/transfer/      passa para outra pessoa (RF-ATD-6)
        POST /{id}/priority/      urgência da fila (RF-ATD-8)
        POST /{id}/add-label/     marca assunto (RF-ATD-9)
        POST /{id}/remove-label/  desmarca assunto
        GET  /counters/           contadores do inbox (RNF-5)
        GET  /agents/?search=     para quem transferir, com a carga de cada um
    """

    model = Conversation
    audit_resource = "Conversation"
    serializer_class = ConversationSerializer
    filterset_class = ConversationFilterset
    ordering_fields = ["last_message_at", "unread_count"]
    select_related = ["contact", "patient", "channel", "assigned_to"]

    def get_queryset(self):
        """
        RECÊNCIA, só recência (RF-ATD-8, revisto em 28/07/2026).

        Ordenava por urgência e depois recência, e o usuário mostrou o preço
        ao vivo: mensagem que acabou de chegar ficava em terceiro lugar,
        embaixo de uma urgente de ONTEM e de uma alta da tarde. Fila de
        atendimento é como toda caixa de entrada — quem falou por último
        aparece primeiro, senão ninguém confia no topo da lista.

        A prioridade não sumiu: ela é a tarja vermelha, o selo e o filtro.
        Marcar não é enterrar o resto.

        A ordenação continua do SERVIDOR porque a fila é paginada: ordenar no
        cliente ordenaria só a página carregada.
        """
        return (
            super()
            .get_queryset()
            .prefetch_related("labels")
            .order_by("-last_message_at")
        )

    @action(detail=True, methods=["post"], url_path="read")
    def read(self, request, pk=None):
        """Marca como lida localmente (RF-INB-4). O `messages/read` no provedor
        entra na Fatia B."""
        conversation = self.get_object()
        if conversation.unread_count:
            conversation.unread_count = 0
            conversation.save(update_fields=["unread_count", "updated_at"])
        # RF-INB-4: além de local, confirma a leitura no provedor.
        from apps.inbox.tasks import mark_whatsapp_read

        mark_whatsapp_read.delay(conversation.pk)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="assign")
    def assign(self, request, pk=None):
        """
        Assume o atendimento (RF-INB-5/8, RF-ATD-15). Sem corpo → para si;
        gestor pode passar `assigned_to` para atribuir a outro.

        `expected_attended_by` (opcional) é o que o cliente VIU na tela: a
        troca fica condicionada a ele, então dois atendentes clicando juntos
        não viram dois donos — o segundo recebe aviso.
        """
        from apps.inbox.attendance import take_over

        conversation = self.get_object()
        assignee = request.user
        assigned_to_id = request.data.get("assigned_to")
        if assigned_to_id:
            assignee = self._resolve_clinic_user(assigned_to_id)

        conversation = self._busy_guard(
            lambda: take_over(
                conversation, assignee, expected=request.data.get("expected_attended_by")
            )
        )
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="mark-waiting")
    def mark_waiting(self, request, pk=None):
        """Devolve para a fila: perde o responsável e volta para Aguardando."""
        from apps.inbox.attendance import mark_waiting

        conversation = mark_waiting(self.get_object(), request.user)
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="resolve")
    def resolve(self, request, pk=None):
        """Encerra (RF-ATD-1.3). `note` opcional vira nota interna."""
        from apps.inbox.attendance import resolve

        conversation = resolve(
            self.get_object(), request.user, note=request.data.get("note", "")
        )
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="reopen")
    def reopen(self, request, pk=None):
        """Reabre à mão (RF-ATD-2) — o inbound já reabre sozinho na ingestão."""
        from apps.inbox.attendance import reopen

        conversation = reopen(self.get_object(), user=request.user)
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="snooze")
    def snooze(self, request, pk=None):
        """Adia até data e hora escolhidas (RF-ATD-1.2)."""
        from django.utils.dateparse import parse_datetime

        from apps.inbox.attendance import snooze

        bruto = request.data.get("until")
        until = parse_datetime(bruto) if bruto else None
        if until is None:
            raise ValidationError({"until": "Informe data e hora (ISO 8601)."})
        if timezone.is_naive(until):
            until = timezone.make_aware(until)
        if until <= timezone.now():
            raise ValidationError({"until": "Escolha um momento no futuro."})

        conversation = snooze(
            self.get_object(), request.user, until=until, note=request.data.get("note", "")
        )
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    # ------------- classificação e passagem adiante (Bloco B1) ---------- #

    @action(detail=True, methods=["post"], url_path="transfer")
    def transfer(self, request, pk=None):
        """Passa o atendimento para outra pessoa (RF-ATD-6). `note` vira nota
        interna e viaja junto no evento."""
        from apps.inbox.attendance import transfer

        destino_id = request.data.get("to")
        if not destino_id:
            raise ValidationError({"to": "Informe para quem transferir."})
        destino = self._resolve_clinic_user(destino_id)

        conversation = self._busy_guard(
            lambda: transfer(
                self.get_object(),
                request.user,
                to_user=destino,
                note=request.data.get("note", ""),
            )
        )
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="priority")
    def priority(self, request, pk=None):
        """Urgência da fila (RF-ATD-8)."""
        from apps.inbox.attendance import set_priority

        valor = request.data.get("priority", ConversationPriority.NORMAL)
        if valor not in ConversationPriority.values:
            raise ValidationError({"priority": "Prioridade desconhecida."})

        conversation = set_priority(self.get_object(), request.user, priority=valor)
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="add-label")
    def add_label(self, request, pk=None):
        from apps.inbox.attendance import add_label

        conversation = add_label(
            self.get_object(), request.user, label=self._resolve_label(request)
        )
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="remove-label")
    def remove_label(self, request, pk=None):
        from apps.inbox.attendance import remove_label

        conversation = remove_label(
            self.get_object(), request.user, label=self._resolve_label(request)
        )
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=False, methods=["get"], url_path="agents")
    def agents(self, request):
        """
        Para quem dá para transferir, com a CARGA de cada um (RF-ATD-6).

        A carga é a única informação que faz a escolha ser uma decisão em vez
        de um chute - sem ela, todo mundo empurra para a mesma pessoa. É também
        o embrião honesto da distribuição do B2: mostra a fila sem prometer
        automatizar nada.

        `?search=` filtra AQUI, não no cliente: buscar é trabalho de servidor.
        Filtrar uma lista já baixada só funciona enquanto ela cabe inteira numa
        resposta - e o dia em que não couber, a busca passa a mentir, achando
        só dentro do pedaço que veio. `LIMITE` é o teto do que se devolve; a
        busca é o caminho para o que ficou de fora.
        """
        from apps.accounts.models import Membership

        abertas = (
            self.model.objects.filter(
                clinic=self.clinic,
                deleted_at__isnull=True,
                status=ConversationStatus.OPEN,
            )
            .values("assigned_to_id")
            .annotate(total=Count("id"))
        )
        carga = {linha["assigned_to_id"]: linha["total"] for linha in abertas}

        pessoas = (
            Membership.objects.filter(clinic=self.clinic, is_active=True)
            .select_related("user")
            .order_by("user__first_name", "user__email")
        )
        termo = request.query_params.get("search", "").strip()
        if termo:
            pessoas = pessoas.filter(
                Q(user__first_name__icontains=termo)
                | Q(user__last_name__icontains=termo)
                | Q(user__email__icontains=termo)
            )
        pessoas = pessoas[:AGENTS_LIMIT]

        return Response(
            [
                {
                    "id": m.user_id,
                    "name": m.user.get_full_name() or m.user.email,
                    "role": m.role,
                    "open_conversations": carga.get(m.user_id, 0),
                }
                for m in pessoas
            ]
        )

    def _resolve_label(self, request):
        from apps.inbox.models import ConversationLabel

        label_id = request.data.get("label")
        if not label_id:
            raise ValidationError({"label": "Informe a etiqueta."})
        label = ConversationLabel.objects.filter(clinic=self.clinic, pk=label_id).first()
        if label is None:
            raise ValidationError({"label": "Etiqueta não encontrada nesta clínica."})
        return label

    # ------------------------------------------------------------------ #

    def _busy_guard(self, fn):
        """Traduz a disputa de posse em erro que a tela sabe explicar."""
        from apps.inbox.attendance import ConversationBusy

        try:
            return fn()
        except ConversationBusy as exc:
            raise PermissionDenied(
                {
                    "detail": "Esta conversa está sendo atendida por outra pessoa.",
                    "code": "conversation_busy",
                    "attended_by": exc.attended_by,
                    "holder": exc.holder,
                }
            ) from exc

    def _notify(self, conversation):
        """Realtime (§12): a fila muda para todo mundo, não só para quem agiu."""
        from apps.inbox.realtime import notify_conversation_updated

        notify_conversation_updated(conversation)

    @action(detail=True, methods=["post"], url_path="link-patient")
    def link_patient(self, request, pk=None):
        """Vincula a conversa a um paciente (RF-INB-7) e garante o vínculo
        contato↔paciente em PatientContact."""
        from apps.patients.models import Patient, PatientContact

        conversation = self.get_object()
        patient_id = request.data.get("patient")
        if not patient_id:
            raise ValidationError({"patient": "Informe o paciente."})
        patient = Patient.objects.filter(clinic=self.clinic, pk=patient_id).first()
        if patient is None:
            raise ValidationError({"patient": "Paciente não encontrado nesta clínica."})

        conversation.patient = patient
        conversation.save(update_fields=["patient", "updated_at"])
        PatientContact.objects.get_or_create(patient=patient, contact=conversation.contact)
        return Response(self.get_serializer(conversation).data)

    @action(detail=False, methods=["get"], url_path="counters")
    def counters(self, request):
        """Contadores do inbox (RNF-5) - endpoint dedicado."""
        queryset = self.get_queryset()
        return Response(
            {
                "total": queryset.count(),
                "unread": queryset.filter(unread_count__gt=0).count(),
                # A fila agora é por STATUS (RF-ATD-1): é isso que vira aba.
                "waiting": queryset.filter(status=ConversationStatus.WAITING).count(),
                "open": queryset.filter(status=ConversationStatus.OPEN).count(),
                "snoozed": queryset.filter(status=ConversationStatus.SNOOZED).count(),
                "resolved": queryset.filter(status=ConversationStatus.RESOLVED).count(),
                "unassigned": queryset.filter(assigned_to__isnull=True).count(),
            }
        )

    def _resolve_clinic_user(self, user_id):
        from apps.accounts.models import Membership

        membership = Membership.objects.filter(clinic=self.clinic, user_id=user_id).first()
        if membership is None:
            raise ValidationError({"assigned_to": "Usuário não pertence a esta clínica."})
        return membership.user
