from django.db.models import Count, F, Q
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response

from apps.core.api.viewsets import ClinicScopedReadOnlyViewSet
from apps.core.health import saude_do_processamento
from apps.core.mixins import AuditMixin
from apps.core.models.audit_log import AuditAction
from apps.inbox.api.filtersets import ConversationFilterset
from apps.inbox.api.serializers import ConversationSerializer
from apps.inbox.choices import AttendedBy, ConversationPriority, ConversationStatus
from apps.inbox.models import Conversation, Message

# Teto do seletor de pessoas. Não é paginação: quem procura alguém fora do
# teto usa a busca, que é o caminho desenhado para isso. Devolver a clínica
# inteira faria uma tela de escolha carregar uma lista que ninguém lê.
AGENTS_LIMIT = 30

# Quanto o painel do contato mostra sem virar uma segunda tela. São tetos, não
# paginação: quem quer o histórico completo abre a conversa antiga; quem quer
# o arquivo antigo rola a thread. O painel é para reconhecer a pessoa rápido.
ARQUIVOS_NO_PAINEL = 12
ATENDIMENTOS_ANTERIORES = 5
NOTAS_DO_ATENDIMENTO = 20


class ConversationViewSet(AuditMixin, ClinicScopedReadOnlyViewSet):
    """
    Lista/detalhe de conversas (RF-INB-1), ordenada por recência. As mutações
    do inbox são ações explícitas (o corpo da conversa é derivado das
    mensagens, não editável direto):

        POST /start/              inicia/encontra a conversa de um paciente ou
                                  telefone (RF-INB-11) - a ÚNICA porta de
                                  criação fora do webhook
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

        ⚠️ `nulls_last` NÃO é detalhe de SQL (11/08/2026). No PostgreSQL,
        `DESC` traz NULL PRIMEIRO, então toda conversa que ainda não tem
        mensagem nenhuma subia ao TOPO da fila, acima da que acabou de chegar.
        Pior: o cliente reordena com NULL por último ao aplicar uma mudança
        local, então a lista trocava de ordem sozinha assim que alguém
        resolvia qualquer conversa. Os dois lados agora ordenam igual.
        """
        from apps.automation.sequence_holds import anotar_sequencias_seguradas

        return anotar_sequencias_seguradas(
            super()
            .get_queryset()
            # A conversa do MODO DE TESTE de fluxo não existe para o Inbox
            # (RF-FLW-25.5): aparecer aqui faria a recepção responder um
            # paciente que não existe.
            .exclude(channel__is_test=True)
            .prefetch_related("labels")
        ).order_by(F("last_message_at").desc(nulls_last=True))

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
        self.log_operation(
            conversation,
            "conversation.assign_other" if assigned_to_id else "conversation.assign",
            assigned_to=assignee.pk,
        )
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="mark-waiting")
    def mark_waiting(self, request, pk=None):
        """Devolve para a fila: perde o responsável e volta para Aguardando."""
        from apps.inbox.attendance import mark_waiting

        conversation = mark_waiting(self.get_object(), request.user)
        self.log_operation(conversation, "conversation.wait")
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="resolve")
    def resolve(self, request, pk=None):
        """Encerra (RF-ATD-1.3). `note` opcional vira nota interna."""
        from apps.inbox.attendance import resolve

        conversation = resolve(
            self.get_object(), request.user, note=request.data.get("note", "")
        )
        self.log_operation(conversation, "conversation.resolve")
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="reopen")
    def reopen(self, request, pk=None):
        """Reabre à mão (RF-ATD-2) — o inbound já reabre sozinho na ingestão."""
        from apps.inbox.attendance import reopen

        conversation = reopen(self.get_object(), user=request.user)
        self.log_operation(conversation, "conversation.reopen")
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
        self.log_operation(conversation, "conversation.snooze", until=until)
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
        self.log_operation(conversation, "conversation.transfer", to=destino.pk)
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
        self.log_operation(conversation, "conversation.priority", priority=valor)
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="add-label")
    def add_label(self, request, pk=None):
        from apps.inbox.attendance import add_label

        label = self._resolve_label(request)
        conversation = add_label(self.get_object(), request.user, label=label)
        self.log_operation(conversation, "conversation.label_add", label=label.pk)
        self._notify(conversation)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="remove-label")
    def remove_label(self, request, pk=None):
        from apps.inbox.attendance import remove_label

        label = self._resolve_label(request)
        conversation = remove_label(self.get_object(), request.user, label=label)
        self.log_operation(conversation, "conversation.label_remove", label=label.pk)
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

    @action(detail=False, methods=["post"], url_path="start")
    def start(self, request):
        """
        Inicia (ou encontra) a conversa de um `patient` OU `phone` (RF-INB-11).

        Criada, nasce Aberta com quem clicou e entra em "Atendendo";
        existente, só devolve — navegar não rouba posse nem reabre Resolvida.
        `created` na resposta diz qual dos dois aconteceu.
        """
        from apps.inbox.services import ConversaSemDestino, iniciar_conversa
        from apps.patients.models import Patient

        patient_id = request.data.get("patient")
        phone = (request.data.get("phone") or "").strip()
        if bool(patient_id) == bool(phone):
            raise ValidationError({"detail": "Informe `patient` OU `phone` (exatamente um)."})

        patient = None
        if patient_id:
            patient = Patient.objects.filter(clinic=self.clinic, pk=patient_id).first()
            if patient is None:
                raise ValidationError({"patient": "Paciente não encontrado nesta clínica."})

        try:
            conversation, created = iniciar_conversa(
                self.clinic, request.user, patient=patient, phone=phone or None
            )
        except ConversaSemDestino as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        if created:
            # Só a criação vira evento: abrir de novo uma conversa que já
            # existia não muda nada, e registrar isso encheria a auditoria de
            # linhas de navegação.
            self.log_operation(
                conversation,
                "conversation.start",
                action=AuditAction.CREATE,
                patient=patient.pk if patient else None,
            )

        data = self.get_serializer(conversation).data
        data["created"] = created
        # A tela avisa quando a conversa NÃO vai no número do próprio paciente
        # (criança sem WhatsApp → número do responsável). O backend é quem
        # sabe: o front da agenda não tem o telefone do paciente à mão.
        data["own_number"] = self._numero_do_proprio(patient, conversation)
        return Response(data, status=201 if created else 200)

    @staticmethod
    def _numero_do_proprio(patient, conversation) -> bool:
        from apps.patients.phone import canonizar_telefone, grafia_alternativa

        if patient is None:
            return True
        canonico = canonizar_telefone(patient.phone)
        if not canonico:
            return False
        grafias = {canonico, grafia_alternativa(canonico) or canonico}
        return conversation.contact.wa_id in grafias

    @action(detail=True, methods=["post"], url_path="link-patient")
    def link_patient(self, request, pk=None):
        """Vincula a conversa a um paciente (RF-INB-7) e garante o vínculo
        contato↔paciente em PatientContact."""
        from apps.patients.vinculos import vincular

        conversation = self.get_object()
        patient = self._paciente_do_corpo(request)

        conversation.patient = patient
        conversation.save(update_fields=["patient", "updated_at"])
        # O primeiro paciente do número vira o principal - regra única em
        # `vinculos.vincular` (antes estava copiada aqui, no /start/ e no sync
        # do EHR, e ninguém virava principal por este caminho: o auto-vínculo
        # da conversa nova (RF-INB-7) nunca acontecia para estes números).
        vincular(patient, conversation.contact)
        self.log_operation(conversation, "conversation.link_patient", patient=patient.pk)
        return Response(self.get_serializer(conversation).data)

    def _paciente_do_corpo(self, request):
        from apps.patients.models import Patient

        patient_id = request.data.get("patient")
        if not patient_id:
            raise ValidationError({"patient": "Informe o paciente."})
        patient = Patient.objects.filter(clinic=self.clinic, pk=patient_id).first()
        if patient is None:
            raise ValidationError({"patient": "Paciente não encontrado nesta clínica."})
        return patient

    @action(detail=True, methods=["post"], url_path="add-contact-patient")
    def add_contact_patient(self, request, pk=None):
        """
        Acrescenta um paciente ao NÚMERO (RF-PAC-7.1) — o segundo filho da
        mesma casa. Não mexe no paciente da conversa: para isso existe o
        `link-patient`, e confundir os dois foi o motivo desta ação existir.
        """
        from apps.patients.vinculos import vincular

        conversation = self.get_object()
        patient = self._paciente_do_corpo(request)
        vincular(patient, conversation.contact)
        self.log_operation(
            conversation,
            "contact.patient_add",
            patient=patient.pk,
            contact=conversation.contact_id,
        )
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="set-primary-patient")
    def set_primary_patient(self, request, pk=None):
        """Troca o principal do número (RF-PAC-7.1): é para ele que vão as
        mensagens que a clínica iniciar por qualquer paciente do contato."""
        from apps.patients.models import PatientContact
        from apps.patients.vinculos import definir_principal

        conversation = self.get_object()
        patient = self._paciente_do_corpo(request)
        try:
            definir_principal(patient, conversation.contact)
        except PatientContact.DoesNotExist as exc:
            raise ValidationError({"patient": str(exc)}) from exc
        self.log_operation(
            conversation,
            "contact.patient_primary",
            patient=patient.pk,
            contact=conversation.contact_id,
        )
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["post"], url_path="remove-contact-patient")
    def remove_contact_patient(self, request, pk=None):
        """
        Tira o paciente do NÚMERO (RF-PAC-7.1). Diferente de `unlink-patient`,
        que solta só a conversa e preserva o vínculo.

        Devolve o que aconteceu junto — quem foi promovido a principal — para a
        tela poder contar em vez de a recepção descobrir depois.
        """
        from apps.patients.models import PatientContact
        from apps.patients.vinculos import desvincular

        conversation = self.get_object()
        patient = self._paciente_do_corpo(request)
        try:
            efeito = desvincular(patient, conversation.contact)
        except PatientContact.DoesNotExist as exc:
            raise ValidationError({"patient": str(exc)}) from exc

        conversation.refresh_from_db()
        promovido = efeito["promoveu"]
        self.log_operation(
            conversation,
            "contact.patient_remove",
            patient=patient.pk,
            contact=conversation.contact_id,
            # Quantas conversas ficaram sem paciente por tabela: o efeito
            # colateral é o que o gestor vai querer entender depois.
            released=efeito["conversas_soltas"],
            promoted=promovido.pk if promovido else None,
        )
        if efeito["conversas_soltas"]:
            self._notify(conversation)
        data = self.get_serializer(conversation).data
        data["promoted_to_primary"] = (
            {"id": promovido.pk, "name": promovido.name} if promovido else None
        )
        return Response(data)

    @action(detail=True, methods=["post"], url_path="unlink-patient")
    def unlink_patient(self, request, pk=None):
        """
        Desfaz o vínculo conversa↔paciente (RF-INB-7).

        Vincular errado acontece — dois pacientes com o mesmo sobrenome, o
        número do filho cadastrado na mãe. Sem desfazer, o engano fica na tela
        para sempre e a recepção passa a desconfiar do vínculo inteiro.

        Solta APENAS esta conversa. O `PatientContact` continua: ele é o
        histórico de que este número já atendeu aquele paciente, e apagá-lo
        junto destruiria o vínculo N:N do responsável familiar (RF-PAC-7) por
        causa de um engano numa conversa.
        """
        conversation = self.get_object()
        if conversation.patient_id is None:
            raise ValidationError("Esta conversa não está vinculada a ninguém.")

        anterior = conversation.patient_id
        conversation.patient = None
        conversation.save(update_fields=["patient", "updated_at"])
        # O paciente que SAIU é a informação da linha: depois do save não há
        # mais como saber de quem a conversa foi desvinculada.
        self.log_operation(conversation, "conversation.unlink_patient", patient=anterior)
        return Response(self.get_serializer(conversation).data)

    @action(detail=True, methods=["get"], url_path="template-context")
    def template_context(self, request, pk=None):
        """
        As fontes de variável desta conversa, JÁ resolvidas (RF-INB-3.1).

        A tela monta a prévia com isto, sem uma ida ao servidor por tecla, e o
        valor vem resolvido daqui para os dois lados produzirem a mesma frase:
        se o front capitalizasse por conta própria, a prévia sairia diferente
        da mensagem enviada.

        ⚠️ Fonte sem dado vem com `value` vazio e `available: false`, em vez de
        sumir da lista. **17 das 25 conversas da clínica real não têm paciente
        vinculado**, e ali as fontes de paciente não resolvem nada - a tela
        precisa mostrar que elas existem e por que não servem, senão o
        atendente escolhe uma e a mensagem sai com buraco no meio da frase.
        """
        from apps.inbox.choices import VariableSource
        from apps.inbox.template_vars import Contexto, valor_da_variavel

        conversation = self.get_object()
        contexto = Contexto.da_conversa(conversation)
        fontes = []
        for fonte in VariableSource.para_contexto("inbox"):
            if fonte == VariableSource.FIXED:
                fontes.append(
                    {
                        "key": fonte.value,
                        "label": fonte.label,
                        "value": "",
                        "available": True,
                        "needs_text": True,
                    }
                )
                continue
            valor = valor_da_variavel({"source": fonte.value}, contexto)
            fontes.append(
                {
                    "key": fonte.value,
                    "label": fonte.label,
                    "value": valor,
                    "available": bool(valor),
                    "needs_text": False,
                }
            )
        return Response({"sources": fontes, "has_patient": conversation.patient_id is not None})

    @action(detail=True, methods=["get"], url_path="contact-panel")
    def contact_panel(self, request, pk=None):
        """
        Quem está do outro lado, em uma chamada só (Bloco C).

        Numa requisição e não em cinco (o caminho do Chatwoot, que carrega por
        acordeão) porque o painel abre inteiro: a recepção não pode esperar
        cinco vezes para saber com quem está falando.
        """
        from apps.inbox.api.serializers.contact_panel import (
            ContactNoteSerializer,
            arquivo_payload,
            atendimento_anterior_payload,
            paciente_payload,
        )
        from apps.inbox.choices import ActivityType
        from apps.patients.models import ContactNote
        from apps.patients.vinculos import pacientes_do_contato

        conversation = self.get_object()
        contact = conversation.contact

        # Um número pode atender vários pacientes (RF-PAC-7, responsável
        # familiar). A recepção precisa ver isso: metade dos enganos de
        # atendimento é responder sobre a pessoa errada da mesma casa.
        vinculos = pacientes_do_contato(contact)
        outros = [
            paciente_payload(v.patient, is_primary=v.is_primary)
            for v in vinculos
            if v.patient_id != conversation.patient_id
        ]
        # O `is_primary` do paciente DA CONVERSA vem do banco, não do default
        # do helper: com a flag fixa em True o filho aparecia como principal
        # quando o principal era a mãe — e é essa flag que o diálogo "para
        # quem é" e a seção "Quem usa este número" usam para o selo.
        principal_do_contato = next(
            (v.patient_id for v in vinculos if v.is_primary), None
        )

        arquivos = (
            Message.objects.filter(conversation=conversation, media__isnull=False)
            .select_related("media")
            .order_by("-wa_timestamp", "-id")[:ARQUIVOS_NO_PAINEL]
        )

        # Atendimentos encerrados, não "conversas anteriores": a conversa é
        # única por contato aqui, então o que tem histórico é o EVENTO de
        # encerramento na linha do tempo.
        anteriores = (
            Message.objects.filter(
                conversation__contact=contact, activity_type=ActivityType.RESOLVED
            )
            .select_related("sent_by")
            .order_by("-wa_timestamp")[:ATENDIMENTOS_ANTERIORES]
        )

        # As notas escritas NO CHAT (RF-ATD-3) também entram no painel — o
        # usuário procurou por elas aqui. São coisas diferentes da nota do
        # contato e continuam separadas: a do atendimento morre com ele, a do
        # contato acompanha a pessoa. Juntar as duas numa lista só apagaria
        # essa diferença, que é o que dá sentido às duas.
        notas_do_chat = (
            Message.objects.filter(conversation=conversation, is_internal=True)
            .select_related("sent_by")
            .order_by("-wa_timestamp")[:NOTAS_DO_ATENDIMENTO]
        )

        return Response(
            {
                "contact": {
                    "id": contact.pk,
                    "wa_id": contact.wa_id,
                    "display_name": contact.display_name,
                },
                "patient": paciente_payload(
                    conversation.patient,
                    is_primary=(
                        conversation.patient_id is not None
                        and conversation.patient_id == principal_do_contato
                    ),
                ),
                "other_patients": outros,
                "notes": ContactNoteSerializer(
                    ContactNote.objects.filter(contact=contact).select_related("author"),
                    many=True,
                ).data,
                "conversation_notes": [
                    {
                        "id": n.pk,
                        "body": n.body,
                        "author_name": (
                            (n.sent_by.get_full_name() or n.sent_by.email)
                            if n.sent_by_id
                            else ""
                        ),
                        "at": n.wa_timestamp,
                        "edited": n.edited_at is not None,
                    }
                    for n in notas_do_chat
                ],
                "files": [arquivo_payload(m, request) for m in arquivos],
                "previous_services": [
                    atendimento_anterior_payload(e) for e in anteriores
                ],
            }
        )

    @action(detail=False, methods=["post"], url_path="check-channel")
    def check_channel(self, request):
        """
        "Já reconectei — verificar": sonda a Meta e cura o canal se der certo.

        É a PORTA DE SAÍDA do canal desconectado, e ela precisa existir porque
        o resto se fecha: o canal só se cura com uma chamada bem-sucedida, mas
        com ele caído o envio e o reenvio ficam bloqueados. Sem uma sonda que
        não seja envio, trocar o token não adiantava — o sistema recusava tudo
        para sempre esperando um sucesso que ele mesmo impedia.

        A sonda NÃO manda mensagem para ninguém: pergunta os dados do número.
        """
        from apps.inbox.services import canal_da_clinica, registrar_saude_do_canal
        from apps.integrations.whatsapp.exceptions import WhatsAppError
        from apps.integrations.whatsapp.registry import get_whatsapp_provider

        # ⚠️ NUNCA o canal de teste: o fake responde que está tudo bem, e a
        # sonda diria "reconectado" com o número de verdade ainda mudo.
        canal = canal_da_clinica(self.clinic)
        if canal is None:
            raise ValidationError("Esta clínica não tem canal de WhatsApp configurado.")

        try:
            dados = get_whatsapp_provider(canal).verify_credentials()
        except WhatsAppError as exc:
            # Continua recusada: conta como falha (o canal já está no chão, e
            # não tem como cair duas vezes) e devolve o motivo REAL da Meta —
            # é ele que diz se o problema é token, número ou permissão.
            registrar_saude_do_canal(canal, erro=exc)
            canal.refresh_from_db()
            return Response(
                {"ok": False, "detail": str(exc), "channel": self._saude_do_canal()}
            )
        except Exception as exc:  # rede, id inexistente, resposta inesperada
            return Response(
                {
                    "ok": False,
                    "detail": f"Não consegui falar com a Meta: {exc}",
                    "channel": self._saude_do_canal(),
                }
            )

        registrar_saude_do_canal(canal)
        return Response({"ok": True, "info": dados, "channel": self._saude_do_canal()})

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
                # ABERTA dividida por QUEM está com ela: é o recorte que os
                # três botões da fila usam, e "aberta" no total não serve —
                # somaria o que a IA conduz com o que a equipe atende.
                "attending": queryset.filter(
                    status=ConversationStatus.OPEN, attended_by=AttendedBy.AGENT
                ).count(),
                "bot": queryset.filter(
                    status=ConversationStatus.OPEN, attended_by=AttendedBy.BOT
                ).count(),
                "snoozed": queryset.filter(status=ConversationStatus.SNOOZED).count(),
                "resolved": queryset.filter(status=ConversationStatus.RESOLVED).count(),
                "unassigned": queryset.filter(assigned_to__isnull=True).count(),
                # Saúde do canal junto: a faixa precisa saber DISSO já na
                # primeira carga da tela — o evento de tempo real só cobre a
                # mudança, e quem abre o Inbox com o canal já morto ficaria
                # sem aviso nenhum.
                "channel": self._saude_do_canal(),
                # Saúde do PROCESSAMENTO. Sem isto, worker parado é invisível
                # até o paciente reclamar — e aí já virou bola de neve.
                "processing": saude_do_processamento(),
            }
        )

    def _saude_do_canal(self) -> dict:
        from apps.inbox.models import Channel
        from apps.inbox.services import canal_da_clinica, mensagens_para_reenviar

        # Quantas ficaram presas. Vai junto com a saúde porque é a mesma
        # faixa que mostra as duas coisas: caída avisa o problema, reconectada
        # oferece o conserto ("3 mensagens não saíram. Reenviar?").
        presas = mensagens_para_reenviar(self.clinic).count()

        canal = canal_da_clinica(self.clinic)
        if canal is None:
            return {
                "configured": False,
                "disconnected": False,
                "reason": "",
                "display_number": "",
                "failed_messages": presas,
            }
        return {
            "configured": True,
            "disconnected": canal.disconnected,
            "reason": canal.disconnect_reason,
            "display_number": canal.display_number,
            "failed_messages": presas,
        }

    def _resolve_clinic_user(self, user_id):
        from apps.accounts.models import Membership

        membership = Membership.objects.filter(clinic=self.clinic, user_id=user_id).first()
        if membership is None:
            raise ValidationError({"assigned_to": "Usuário não pertence a esta clínica."})
        return membership.user
