from django.db.models import Count, Q
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.exceptions import PermissionDenied, ValidationError
from rest_framework.response import Response
from rest_framework.status import HTTP_201_CREATED, HTTP_400_BAD_REQUEST

from apps.automation.api.serializers import FlowRunSerializer, FlowSerializer, FlowVersionSerializer
from apps.automation.choices import FlowRunStatus, FlowStatus
from apps.automation.graph import validate_graph
from apps.automation.models import Flow, FlowRun, SequenceStep
from apps.core.api.permissions import IsClinicManager, IsClinicMember
from apps.core.api.viewsets import ClinicScopedModelViewSet, ClinicScopedReadOnlyViewSet
from apps.core.mixins import AuditMixin
from apps.core.models.audit_log import AuditAction


class FlowViewSet(AuditMixin, ClinicScopedModelViewSet):
    """
    Fluxos de atendimento (§4.3.2).

    Só o GESTOR, e para ler também: um fluxo mal montado responde no lugar da
    clínica para todo paciente que escrever. Não é catálogo como as etiquetas,
    onde o atendente precisa escolher.
    """

    model = Flow
    audit_resource = "Flow"
    serializer_class = FlowSerializer
    permission_classes = [IsClinicManager]
    # Montar fluxo é do gestor; DISPARAR um é de quem atende (RF-FLW-22.2).
    # O parceiro segue barrado: sem `partner_allowed`, o `IsClinicMember`
    # fecha a view para ele.
    action_permission_classes = {
        "available": [IsClinicMember],
        "start": [IsClinicMember],
    }
    ordering_fields = ["priority", "name"]

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .select_related("current_version")
            .annotate(
                runs_active=Count(
                    "runs",
                    filter=Q(
                        runs__status=FlowRunStatus.ACTIVE,
                        runs__deleted_at__isnull=True,
                        # Execução de teste não é atendimento (RF-FLW-25.5).
                        runs__is_test=False,
                    ),
                    distinct=True,
                )
            )
            .order_by("priority", "name")
        )

    @action(detail=True, methods=["get"], url_path="export")
    def export(self, request, pk=None):
        """
        O fluxo em um arquivo, para levar a outra clínica (RF-FLW-24).

        ⚠️ O que sai NÃO tem id de sequência nem de etiqueta: eles viram nome.
        Id de outra clínica não é só inútil, é perigoso — o mesmo número existe
        lá apontando para outra coisa, e o fluxo importado marcaria a etiqueta
        errada no paciente errado.
        """
        from apps.automation.portability import exportar

        flow = self.get_object()
        # LEITURA, não alteração: o desenho do fluxo sai daqui em arquivo, e
        # levar o atendimento da clínica para fora é o que a linha registra.
        self.log_operation(flow, "flow.export", action=AuditAction.READ)
        return Response(exportar(flow))

    @action(detail=False, methods=["post"], url_path="import")
    def importar_fluxo(self, request):
        """
        Cria aqui o fluxo exportado de outra clínica (RF-FLW-24.1).

        Nasce em RASCUNHO com a lista do que não existe nesta clínica: publicar
        sozinho poria no ar um fluxo que fala com paciente sem ninguém ter
        olhado, e o arquivo pode citar sequência e modelo que só existem lá.
        """
        from apps.automation.portability import ArquivoInvalido, importar

        arquivo = request.data.get("arquivo")
        if arquivo is None:
            raise ValidationError({"arquivo": "Envie o arquivo do fluxo."})
        try:
            flow, pendencias = importar(
                self.clinic, arquivo, nome=request.data.get("nome", "")
            )
        except ArquivoInvalido as exc:
            raise ValidationError({"arquivo": str(exc)}) from exc

        self.log_operation(
            flow,
            "flow.import",
            action=AuditAction.CREATE,
            pending=len(pendencias),
        )
        return Response(
            {
                **self.get_serializer(flow).data,
                # A tela mostra isto ANTES de a pessoa procurar o que quebrou.
                "pendencias": pendencias,
            },
            status=HTTP_201_CREATED,
        )

    def perform_destroy(self, instance):
        """
        Apagar um fluxo tem duas guardas, e as duas falam (RF-FLW-26).

        Ativo não se apaga: tem paciente podendo cair nele agora, e sumir com
        o fluxo no meio é o robô mudo sem aviso. Desativar primeiro é o ato
        consciente. E fluxo que é passo de sequência não se apaga: o RESTRICT
        do banco estouraria um 500, e a mensagem de banco não diz à clínica
        QUAL sequência segura o fluxo.
        """
        if instance.status == FlowStatus.ACTIVE:
            raise ValidationError(
                {"detail": "Este fluxo está ativo. Desative antes de apagar."}
            )
        donos = list(
            SequenceStep.objects.filter(flow=instance)
            .select_related("sequence")
            .values_list("sequence__name", flat=True)
            .distinct()
        )
        if donos:
            nomes = ", ".join(sorted(set(donos)))
            raise ValidationError(
                {
                    "detail": (
                        f"Este fluxo é um passo da sequência {nomes}. "
                        "Troque o fluxo do passo antes de apagar."
                    )
                }
            )
        super().perform_destroy(instance)

    @action(detail=True, methods=["post"], url_path="activate")
    def activate(self, request, pk=None):
        """
        Publica o fluxo (RF-FLW-3/4).

        A validação é a PORTA daqui: rascunho salva quebrado de propósito,
        porque montar um fluxo é trabalho de várias sessões, mas ativo tem
        paciente do outro lado. Os problemas voltam em frases de gente, para
        o gestor consertar sem precisar entender de grafo.
        """
        flow = self.get_object()
        version = flow.current_version

        if not version:
            return Response(
                {"detail": "O fluxo ainda não tem desenho.", "problems": ["O fluxo está vazio."]},
                status=HTTP_400_BAD_REQUEST,
            )

        problems = validate_graph(version.graph or {}, self.clinic)
        if problems:
            return Response(
                {"detail": "O fluxo tem pendências.", "problems": problems},
                status=HTTP_400_BAD_REQUEST,
            )

        agora = timezone.now()
        flow.status = FlowStatus.ACTIVE
        flow.activated_at = agora
        version.published_at = version.published_at or agora
        version.save(update_fields=["published_at", "updated_at"])
        flow.save(update_fields=["status", "activated_at", "updated_at"])
        self.log_operation(flow, "flow.activate", version=version.pk)
        return Response(self.get_serializer(self.get_queryset().get(pk=flow.pk)).data)

    @action(detail=True, methods=["post"], url_path="deactivate")
    def deactivate(self, request, pk=None):
        """
        Volta para rascunho. As execuções em voo NÃO são interrompidas: elas
        seguem na versão em que começaram até terminar ou cair no timeout -
        cortar no meio deixaria o paciente falando sozinho.
        """
        flow = self.get_object()
        flow.status = FlowStatus.DRAFT
        flow.save(update_fields=["status", "updated_at"])
        self.log_operation(flow, "flow.deactivate")
        return Response(self.get_serializer(self.get_queryset().get(pk=flow.pk)).data)

    # ------------------------------------------------------------------ #
    # Modo de teste (RF-FLW-25): o gestor conversa com o rascunho.
    # ------------------------------------------------------------------ #

    @action(detail=True, methods=["post", "delete"], url_path="teste")
    def teste(self, request, pk=None):
        """POST abre ou zera a sessão; DELETE encerra e apaga o rastro."""
        from apps.automation import teste as modo_teste

        flow = self.get_object()
        if request.method == "DELETE":
            modo_teste.encerrar_teste(flow)
            return Response({"detail": "Teste encerrado."})
        return Response(modo_teste.iniciar_teste(flow))

    @action(detail=True, methods=["post"], url_path="teste/falar")
    def teste_falar(self, request, pk=None):
        """O gestor fala como o paciente: texto, ou o toque num botão."""
        from apps.automation import teste as modo_teste

        flow = self.get_object()
        texto = (request.data.get("texto") or "").strip()
        botao = (request.data.get("botao") or "").strip()
        titulo = (request.data.get("titulo") or "").strip()
        if not texto and not botao:
            raise ValidationError({"texto": "Diga alguma coisa, como o paciente diria."})
        return Response(
            modo_teste.falar_no_teste(
                flow, texto=titulo if botao else texto, interactive_id=botao
            )
        )

    @action(detail=True, methods=["post"], url_path="teste/comecar")
    def teste_comecar(self, request, pk=None):
        """Fluxo de disparo manual: o botão "Começar como a recepção"."""
        from apps.automation import teste as modo_teste

        flow = self.get_object()
        return Response(modo_teste.comecar_manual(flow))

    @action(detail=True, methods=["post"], url_path="teste/pular-espera")
    def teste_pular_espera(self, request, pk=None):
        from apps.automation import teste as modo_teste

        flow = self.get_object()
        return Response(modo_teste.pular_espera(flow))

    @action(detail=True, methods=["get"], url_path="versions")
    def versions(self, request, pk=None):
        flow = self.get_object()
        return Response(FlowVersionSerializer(flow.versions.order_by("-number"), many=True).data)

    # ------------------------------------------------------------------ #
    # Disparo à mão (RF-FLW-22)
    #
    # As duas ações abaixo são do ATENDENTE, não do gestor: quem devolve a
    # conversa para o robô é quem está com ela na mão. Moram aqui, e não no
    # viewset de conversa, porque o Inbox não conhece a automação - a seta
    # aponta num sentido só (RF-FLW-21), e invertê-la por uma ação de tela
    # juntaria os dois apps.
    # ------------------------------------------------------------------ #

    @action(detail=False, methods=["get"], url_path="available")
    def available(self, request):
        """
        Os fluxos que dá para disparar à mão (RF-FLW-22.4/22.6).

        Entra qualquer fluxo ativo COM versão publicada, seja qual for o
        gatilho: o gatilho diz quando o fluxo começa sozinho, e à mão é outra
        porta. Rascunho fica de fora, porque disparar um fluxo pela metade é
        pior do que não ter nenhum.

        `clinic_open` vem junto para a tela poder avisar que um fluxo de fora
        do horário nunca começaria sozinho agora. É aviso, não trava
        (RF-FLW-22.5).
        """
        from apps.automation.engine import _clinic_is_open

        fluxos = (
            Flow.objects.filter(
                clinic=self.clinic, deleted_at__isnull=True, status=FlowStatus.ACTIVE
            )
            .exclude(current_version__isnull=True)
            .select_related("current_version")
            .order_by("priority", "name")
        )
        return Response(
            {
                "clinic_open": _clinic_is_open(self.clinic),
                "results": [
                    {
                        "id": f.pk,
                        "name": f.name,
                        "trigger": f.trigger,
                        "trigger_config": f.trigger_config or {},
                        "only_outside_hours": f.only_outside_hours,
                        "steps": len((f.current_version.graph or {}).get("nodes") or []),
                    }
                    for f in fluxos
                ],
            }
        )

    @action(detail=True, methods=["post"], url_path="start")
    def start(self, request, pk=None):
        """
        Passa uma conversa para este fluxo (RF-FLW-22).

        O fluxo começa do INÍCIO e a conversa deixa de ser de quem mandou. A
        execução que ficou pausada quando o atendente assumiu não é retomada:
        depois de dez minutos de conversa humana, o robô voltar perguntando o
        que já foi respondido é pior do que recomeçar (RF-FLW-22.1).
        """
        from apps.automation.engine import start_run
        from apps.inbox.api.serializers import ConversationSerializer
        from apps.inbox.models import Conversation
        from apps.inbox.realtime import notify_conversation_updated

        flow = self.get_object()
        if flow.status != FlowStatus.ACTIVE or not flow.current_version:
            raise ValidationError({"flow": "Este fluxo não está publicado."})

        conversa_id = request.data.get("conversation")
        if not conversa_id:
            raise ValidationError({"conversation": "Informe a conversa."})
        conversation = Conversation.objects.filter(
            clinic=self.clinic, pk=conversa_id, deleted_at__isnull=True
        ).first()
        if conversation is None:
            raise ValidationError({"conversation": "Conversa não encontrada nesta clínica."})

        run = start_run(flow, conversation, by_user=request.user)
        if run is None:
            # Não deu para tomar a caneta: ou ela não está com quem mandou, ou
            # o contato já tem execução ativa. O corpo é o MESMO do erro de
            # posse do Inbox, achatado, porque a tela já sabe traduzir este.
            conversation.refresh_from_db()
            raise PermissionDenied(
                {
                    "detail": "Esta conversa não está com você.",
                    "code": "conversation_busy",
                    "attended_by": conversation.attended_by,
                    "holder": getattr(conversation.assigned_to, "pk", None),
                }
            )

        notify_conversation_updated(conversation)
        return Response(ConversationSerializer(conversation, context={"request": request}).data)


class FlowRunViewSet(ClinicScopedReadOnlyViewSet):
    """
    Execuções (RF-FLW-12). Somente leitura: quem move a execução é o motor.

    Existe para o gestor responder "em que pergunta as pessoas desistem", que
    é a única métrica que faz alguém melhorar um fluxo depois de montado.
    """

    model = FlowRun
    serializer_class = FlowRunSerializer
    permission_classes = [IsClinicManager]

    def get_queryset(self):
        queryset = super().get_queryset().select_related("flow", "contact").order_by("-created_at")
        flow = self.request.query_params.get("flow")
        if flow:
            queryset = queryset.filter(flow_id=flow)
        status = self.request.query_params.get("status")
        if status:
            queryset = queryset.filter(status__in=status.split(","))
        return queryset
