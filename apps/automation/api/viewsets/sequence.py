from datetime import timedelta

from django.db.models import Count, F, Q
from django.utils import timezone
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError
from rest_framework.response import Response
from rest_framework.status import HTTP_201_CREATED

from apps.automation.api.serializers import (
    ProximoDisparoSerializer,
    SequenceEnrollmentSerializer,
    SequenceSerializer,
    SequenceStepSerializer,
)
from apps.automation.choices import (
    EnrollmentEndReason,
    EnrollmentSource,
    FlowRunEventType,
    FlowTrigger,
    SequenceDispatchStatus,
    SequenceEnrollmentStatus,
)
from apps.automation.modelos import aplicar_modelo, catalogo, criar_fluxo_de_aviso
from apps.automation.models import (
    FlowRunEvent,
    Sequence,
    SequenceDispatch,
    SequenceEnrollment,
    SequenceStep,
)
from apps.automation.sequences import (
    MAX_POR_LOTE,
    reapontar_apos_apagar_passo,
    SemContato,
    contato_do_paciente,
    inscrever,
    inscrever_em_lote,
    normalizar_ordem,
    remover,
    resultado_por_passo,
)
from apps.core.api.permissions import IsClinicManager, IsClinicMember
from apps.patients.phone import so_digitos
from apps.core.api.viewsets import ClinicScopedModelViewSet
from apps.core.mixins import AuditMixin

# Quantas linhas o painel mostra de próximos disparos. É uma AMOSTRA do que vem
# pela frente, não a fila inteira: com 1.758 inscritos numa trilha, a lista
# completa não cabe na tela nem responde a pergunta que ela existe para
# responder, que é "o que sai agora e o que está preso".
PROXIMOS_NO_PAINEL = 25

# Janela do relatório de motivos, em dias.
JANELA_DO_RELATORIO = 30


class SequenceViewSet(AuditMixin, ClinicScopedModelViewSet):
    """
    Sequências (§4.4).

    Montar é do GESTOR (RF-SEQ-10): uma sequência mal montada fala com centenas
    de pacientes sem ninguém no meio. Inscrever e remover são de quem atende, e
    ficam nas ações abaixo. O parceiro não entra em nenhuma das duas, porque
    sem `partner_allowed` o `IsClinicMember` já fecha a view para ele.
    """

    model = Sequence
    audit_resource = "Sequence"
    serializer_class = SequenceSerializer
    permission_classes = [IsClinicManager]
    # Montar é do gestor; LER é de quem atende (RF-SEQ-10).
    #
    # ⚠️ Diferente de Fluxos, onde nem a leitura é do atendente. Duas razões:
    # o painel existe para responder "por que o paciente não recebeu?", e quem
    # recebe essa pergunta é a recepção; e inscrever pela ficha, que é ação
    # dele, exige LISTAR as trilhas para escolher uma. Sem a listagem, a porta
    # da ficha não teria o que oferecer.
    action_permission_classes = {
        "list": [IsClinicMember],
        "retrieve": [IsClinicMember],
        "summary": [IsClinicMember],
        "dispatches": [IsClinicMember],
        "results": [IsClinicMember],
        "templates": [IsClinicMember],
        "of_patient": [IsClinicMember],
        "enrollments": [IsClinicMember],
        "report": [IsClinicMember],
        "enroll": [IsClinicMember],
        "enroll_batch": [IsClinicMember],
        "unenroll": [IsClinicMember],
    }
    ordering_fields = ["name"]

    def get_queryset(self):
        return (
            super()
            .get_queryset()
            .prefetch_related("steps__flow")
            .annotate(
                active_enrollments=Count(
                    "enrollments",
                    filter=Q(
                        enrollments__status=SequenceEnrollmentStatus.ACTIVE,
                        enrollments__deleted_at__isnull=True,
                    ),
                    distinct=True,
                )
            )
            .order_by("name")
        )

    def retrieve(self, request, *args, **kwargs):
        """
        O detalhe traz as contagens POR PASSO, que a listagem não traz.

        Separado de propósito: são três agregações por passo, e cobrá-las na
        lista faria a tela de entrada pagar pelo detalhe de trilhas que ninguém
        abriu.
        """
        sequence = self.get_object()
        dados = self.get_serializer(sequence).data
        dados["steps"] = SequenceStepSerializer(
            self._passos_com_contagem(sequence), many=True
        ).data
        return Response(dados)

    def _passos_com_contagem(self, sequence):
        """
        Passos na ordem do RELÓGIO, cada um com parados, saídos e pulados.

        ⚠️ Duas consultas agrupadas em vez de três `Count` anotados no mesmo
        queryset: as anotações passariam por relações diferentes na mesma
        junção, e o produto cartesiano só não estraga o número por causa do
        `distinct`, que custa caro à toa. Aqui é uma varredura por tabela.
        """
        passos = list(
            sequence.steps.select_related("flow").order_by(
                "offset_days", "send_time", "order"
            )
        )

        parados = dict(
            SequenceEnrollment.objects.filter(
                sequence=sequence,
                status=SequenceEnrollmentStatus.ACTIVE,
                current_step__isnull=False,
            )
            .values_list("current_step")
            .annotate(total=Count("id"))
        )
        resolvidos = {
            (linha["step"], linha["status"]): linha["total"]
            for linha in SequenceDispatch.objects.filter(enrollment__sequence=sequence)
            .values("step", "status")
            .annotate(total=Count("id"))
        }

        for passo in passos:
            passo.parados = parados.get(passo.pk, 0)
            passo.saidos = resolvidos.get((passo.pk, SequenceDispatchStatus.STARTED), 0)
            passo.pulados = resolvidos.get((passo.pk, SequenceDispatchStatus.SKIPPED), 0)
        return passos

    @action(detail=False, methods=["get"], url_path="templates")
    def templates(self, request):
        """Os modelos de trilha oferecidos na criação (RF-SEQ-12)."""
        return Response(catalogo())

    # Os três recortes de "quem está dentro" (RF-SEQ-11.4). São as três
    # perguntas reais: quem ainda vai receber, quem travou, e quem já saiu.
    RECORTES = ("correndo", "parados", "sairam")
    POR_PAGINA = 30

    @action(detail=True, methods=["get"], url_path="enrollments")
    def enrollments(self, request, pk=None):
        """
        A lista de quem está dentro, paginada e com busca (RF-SEQ-11.4).

        Diferente de `dispatches/`, que é a AMOSTRA dos 25 mais próximos e
        responde "o que sai hoje": aqui a pergunta é "onde está a Maria" e
        "tira essa pessoa daqui", e para isso a lista precisa ser inteira.

        As três contagens vêm SEMPRE, e não só a do recorte pedido: elas ficam
        nos próprios botões, e saber que há 14 parados já muda o que a pessoa
        vai fazer antes de clicar.
        """
        sequence = self.get_object()
        recorte = request.query_params.get("estado") or "correndo"
        if recorte not in self.RECORTES:
            raise ValidationError({"estado": f"Use um de {', '.join(self.RECORTES)}."})

        base = SequenceEnrollment.objects.filter(sequence=sequence)
        contagens = {
            "correndo": base.filter(
                status=SequenceEnrollmentStatus.ACTIVE, held_since__isnull=True
            ).count(),
            "parados": base.filter(
                status=SequenceEnrollmentStatus.ACTIVE, held_since__isnull=False
            ).count(),
            "sairam": base.exclude(status=SequenceEnrollmentStatus.ACTIVE).count(),
        }

        if recorte == "correndo":
            linhas = base.filter(
                status=SequenceEnrollmentStatus.ACTIVE, held_since__isnull=True
            ).order_by(F("next_dispatch_at").asc(nulls_last=True))
        elif recorte == "parados":
            linhas = base.filter(
                status=SequenceEnrollmentStatus.ACTIVE, held_since__isnull=False
            ).order_by("held_since")
        else:
            linhas = base.exclude(status=SequenceEnrollmentStatus.ACTIVE).order_by(
                "-updated_at"
            )

        busca = (request.query_params.get("search") or "").strip()
        if busca:
            # Pelo nome da ficha, pelo nome salvo no WhatsApp ou pelo número.
            # ⚠️ As três, porque a lista mostra linha SEM ficha: procurar só em
            # `patient__name` faria a busca não achar quem a tela exibe.
            linhas = linhas.filter(
                Q(patient__name__icontains=busca)
                | Q(contact__display_name__icontains=busca)
                | Q(contact__wa_id__icontains=so_digitos(busca) or busca)
            )

        total = linhas.count()
        try:
            offset = max(0, int(request.query_params.get("offset") or 0))
        except ValueError:
            offset = 0
        pagina = list(
            linhas.select_related("patient", "contact", "current_step")[
                offset : offset + self.POR_PAGINA
            ]
        )

        # A posição do passo sai de UMA lista, montada uma vez: pedir os passos
        # por linha seria uma consulta por pessoa.
        passos = [p for p in sequence.steps.all() if p.is_active]
        passos.sort(key=lambda p: (p.offset_days, p.send_time, p.order))
        posicao = {p.pk: i + 1 for i, p in enumerate(passos)}

        return Response(
            {
                "contagens": contagens,
                "total": total,
                "tem_mais": offset + len(pagina) < total,
                "resultados": [
                    self._linha_de_quem_esta_dentro(e, posicao, len(passos))
                    for e in pagina
                ],
            }
        )

    @staticmethod
    def _linha_de_quem_esta_dentro(enrollment, posicao, passos_total):
        """
        Uma linha da lista.

        ⚠️ `sem_ficha` existe para a tela mostrar o NÚMERO em vez de esconder a
        linha: quem entra na trilha é o contato, e nem todo contato virou
        paciente. Escondendo, a lista não bateria com a contagem do topo, que é
        a divergência que a tela de resgate já ensinou a não repetir.
        """
        contact = enrollment.contact
        tem_ficha = bool(enrollment.patient_id)
        return {
            "id": enrollment.pk,
            "quem": (
                enrollment.patient.name
                if tem_ficha
                else (contact.display_name or contact.wa_id)
            ),
            "numero": contact.wa_id,
            "sem_ficha": not tem_ficha,
            "patient": enrollment.patient_id,
            "passo_nome": (
                enrollment.current_step.name if enrollment.current_step_id else ""
            ),
            "passo_numero": posicao.get(enrollment.current_step_id, 0),
            "passos_total": passos_total,
            "source": enrollment.source,
            "entrou_em": enrollment.created_at,
            "next_dispatch_at": enrollment.next_dispatch_at,
            "held_since": enrollment.held_since,
            "hold_reason": enrollment.hold_reason,
            "end_reason": enrollment.end_reason,
            "saiu_em": (
                enrollment.updated_at
                if enrollment.status != SequenceEnrollmentStatus.ACTIVE
                else None
            ),
        }

    @action(detail=False, methods=["get"], url_path="of-patient")
    def of_patient(self, request):
        """
        As sequências de UM paciente, para o cartão da ficha (RF-SEQ-3.2).

        Uma chamada só, porque a ficha precisa das três coisas juntas: em quais
        trilhas a pessoa está agora, por onde já passou, e o contexto do número
        (sem WhatsApp, pediu silêncio, número usado por mais de um paciente).
        Buscar isso em três requisições faria o cartão piscar em três tempos.
        """
        from apps.patients.models import Patient, PatientContact

        patient_id = request.query_params.get("patient")
        patient = Patient.objects.filter(pk=patient_id, clinic=self.clinic).first()
        if patient is None:
            raise ValidationError({"patient": "Paciente não encontrado nesta clínica."})

        try:
            contact = contato_do_paciente(patient)
        except SemContato:
            contact = None

        # ⚠️ Pelo PACIENTE **ou** pelo contato. Quem entra na trilha é o número,
        # e inscrição nascida de nó de fluxo costuma ter só o contato: filtrar
        # apenas por paciente faria a ficha dizer "não está em nenhuma" com o
        # número dentro de uma sequência mandando mensagem.
        de_quem = Q(patient=patient)
        if contact is not None:
            de_quem |= Q(contact=contact)
        inscricoes = (
            SequenceEnrollment.objects.filter(de_quem, sequence__clinic=self.clinic)
            .select_related("sequence", "current_step")
            .prefetch_related("sequence__steps")
            .order_by("-created_at")
        )

        ativas, historico = [], []
        for enrollment in inscricoes:
            if enrollment.status == SequenceEnrollmentStatus.ACTIVE:
                ativas.append(self._trilha_ativa(enrollment))
            else:
                historico.append(
                    {
                        "id": enrollment.pk,
                        "sequence": enrollment.sequence_id,
                        "sequence_name": enrollment.sequence.name,
                        "end_reason": enrollment.end_reason,
                        "quando": enrollment.updated_at,
                    }
                )

        return Response(
            {
                "contato": {
                    "id": contact.pk if contact else None,
                    "wa_id": contact.wa_id if contact else "",
                    "opt_out": bool(contact and contact.marketing_opt_out),
                    # Mãe e filho no mesmo telefone é comum, e quem entra na
                    # trilha é o NÚMERO: sem isto a recepção tira uma pessoa e
                    # derruba a outra sem saber.
                    "pacientes_no_numero": (
                        PatientContact.objects.filter(contact=contact).count()
                        if contact
                        else 0
                    ),
                },
                "ativas": ativas,
                "historico": historico,
            }
        )

    @staticmethod
    def _trilha_ativa(enrollment):
        """
        Uma inscrição ativa, com o passo dito como posição e o que falta.

        ⚠️ `current_step` é o passo que AINDA VAI disparar, não o último que
        saiu. Então ele conta dentro de "faltam": dizer que faltam 2 quando a
        próxima mensagem ainda é a do passo atual erraria por um, e é sobre
        esse número que a recepção decide tirar alguém da trilha.
        """
        passos = [p for p in enrollment.sequence.steps.all() if p.is_active]
        passos.sort(key=lambda p: (p.offset_days, p.send_time, p.order))

        numero, faltam = 0, 0
        if enrollment.current_step_id:
            ids = [p.pk for p in passos]
            if enrollment.current_step_id in ids:
                numero = ids.index(enrollment.current_step_id) + 1
                faltam = len(passos) - numero + 1

        return {
            "id": enrollment.pk,
            "sequence": enrollment.sequence_id,
            "sequence_name": enrollment.sequence.name,
            "marketing": enrollment.sequence.is_marketing,
            "sequencia_ligada": enrollment.sequence.is_active,
            "passo_atual": (
                enrollment.current_step.name or f"Passo {numero}"
                if enrollment.current_step_id
                else ""
            ),
            "passo_numero": numero,
            "passos_total": len(passos),
            "faltam": faltam,
            "next_dispatch_at": enrollment.next_dispatch_at,
            "held_since": enrollment.held_since,
            "hold_reason": enrollment.hold_reason,
            "source": enrollment.source,
        }

    def perform_create(self, serializer):
        """
        Cria a trilha e, quando veio de um modelo, os passos dele.

        ⚠️ Nasce DESLIGADA sempre (`is_active` não é aceito na criação):
        ligar é decisão de quem gerencia, e uma trilha que começa a falar no
        instante em que foi criada não deu a ninguém a chance de conferir.
        """
        sequence = serializer.save(clinic=self.clinic, is_active=False)
        slug = (self.request.data.get("template") or "").strip()
        if slug:
            aplicar_modelo(sequence, slug)

    @action(detail=False, methods=["get"], url_path="summary")
    def summary(self, request):
        """
        O agregado da clínica, que a lista mostra antes de abrir qualquer coisa
        (RF-SEQ-11.1).

        ⚠️ Conta em TODAS as trilhas, inclusive as desligadas: "quantos estão
        em trilhas" é a pergunta de quem quer saber o alcance, e esconder os de
        uma trilha pausada daria um número que não bate com a soma das linhas.
        """
        agora = timezone.now()
        inicio_do_dia = agora - timedelta(hours=24)
        inscricoes = SequenceEnrollment.objects.filter(clinic=self.clinic)

        return Response(
            {
                "em_trilhas": inscricoes.filter(
                    status=SequenceEnrollmentStatus.ACTIVE
                ).count(),
                "disparos_hoje": SequenceDispatch.objects.filter(
                    enrollment__clinic=self.clinic,
                    status=SequenceDispatchStatus.STARTED,
                    resolved_at__gte=inicio_do_dia,
                ).count(),
                "segurados_agora": inscricoes.filter(
                    status=SequenceEnrollmentStatus.ACTIVE,
                    held_since__isnull=False,
                ).count(),
                # RF-SEQ-11.3: quantas EXECUÇÕES nascidas de sequência tiveram
                # resposta. Por execução, não por mensagem: responder três
                # vezes é um paciente que respondeu.
                "responderam": FlowRunEvent.objects.filter(
                    run__in=SequenceDispatch.objects.filter(
                        enrollment__clinic=self.clinic,
                        resolved_at__gte=agora - timedelta(days=JANELA_DO_RELATORIO),
                        flow_run__isnull=False,
                    ).values("flow_run"),
                    event_type=FlowRunEventType.REPLIED,
                )
                .values("run")
                .distinct()
                .count(),
            }
        )

    @action(detail=True, methods=["get"], url_path="dispatches")
    def dispatches(self, request, pk=None):
        """
        Os próximos disparos, com os SEGURADOS primeiro (RF-SEQ-11).

        Segurado antes de "na fila" porque é o único que pede decisão de gente:
        o resto vai sair sozinho na hora marcada.
        """
        sequence = self.get_object()
        proximos = (
            SequenceEnrollment.objects.filter(
                sequence=sequence,
                status=SequenceEnrollmentStatus.ACTIVE,
                next_dispatch_at__isnull=False,
            )
            .select_related("patient", "contact", "current_step")
            .order_by(F("held_since").asc(nulls_last=True), "next_dispatch_at")[
                :PROXIMOS_NO_PAINEL
            ]
        )
        return Response(ProximoDisparoSerializer(proximos, many=True).data)

    @action(detail=True, methods=["get"], url_path="results")
    def results(self, request, pk=None):
        """
        Entregues, lidas, respondidas e agendadas por passo (RF-SEQ-11.3).

        Separado do detalhe porque é a conta mais cara do painel e a que menos
        gente olha: quem abre a trilha para ver o que está segurado não precisa
        pagar por ela.
        """
        sequence = self.get_object()
        return Response(
            {
                "dias": JANELA_DO_RELATORIO,
                "janela_do_agendou": sequence.conversion_days,
                "passos": [
                    {"step": step_id, **contas}
                    for step_id, contas in resultado_por_passo(
                        sequence, dias=JANELA_DO_RELATORIO
                    ).items()
                ],
            }
        )

    @action(detail=True, methods=["get"], url_path="report")
    def report(self, request, pk=None):
        """
        O que aconteceu (RF-SEQ-11): por que não saiu, e quem saiu da trilha.

        É a resposta a "por que o paciente não recebeu?", que sem isto só
        existe no banco.
        """
        sequence = self.get_object()
        desde = timezone.now() - timedelta(days=JANELA_DO_RELATORIO)

        pulos = (
            SequenceDispatch.objects.filter(
                enrollment__sequence=sequence,
                status=SequenceDispatchStatus.SKIPPED,
                resolved_at__gte=desde,
            )
            .values("skip_reason")
            .annotate(total=Count("id"))
            .order_by("-total")
        )
        saidas = (
            SequenceEnrollment.objects.filter(sequence=sequence, updated_at__gte=desde)
            .exclude(status=SequenceEnrollmentStatus.ACTIVE)
            .values("end_reason")
            .annotate(total=Count("id"))
            .order_by("-total")
        )
        return Response(
            {
                "dias": JANELA_DO_RELATORIO,
                "pulos": [
                    {"motivo": linha["skip_reason"], "total": linha["total"]}
                    for linha in pulos
                    if linha["skip_reason"]
                ],
                "saidas": [
                    {"motivo": linha["end_reason"], "total": linha["total"]}
                    for linha in saidas
                    if linha["end_reason"]
                ],
            }
        )

    @action(detail=True, methods=["post"], url_path="enroll")
    def enroll(self, request, pk=None):
        """
        Inscreve um paciente à mão, pela ficha (RF-SEQ-3.2).

        Recusas explicadas em vez de silêncio: paciente sem número, contato que
        pediu para não receber e inscrição que já existia respondem 400 com o
        motivo escrito - quem está na tela precisa saber por que não entrou.
        """
        sequence = self.get_object()
        patient = self._paciente(request)

        try:
            contact = contato_do_paciente(patient)
        except SemContato as erro:
            raise ValidationError({"detail": str(erro)}) from erro

        if sequence.is_marketing and contact.marketing_opt_out:
            raise ValidationError(
                {"detail": "Este contato pediu para não receber mensagens de marketing."}
            )

        enrollment = inscrever(
            sequence,
            contact,
            source=EnrollmentSource.PATIENT_RECORD,
            patient=patient,
        )
        if enrollment is None:
            raise ValidationError(
                {"detail": "Este paciente já está nesta sequência, ou ela não tem passo ativo."}
            )
        return Response(
            SequenceEnrollmentSerializer(enrollment).data, status=HTTP_201_CREATED
        )

    @action(detail=True, methods=["post"], url_path="enroll-batch")
    def enroll_batch(self, request, pk=None):
        """
        Inscreve uma SELEÇÃO inteira, vinda da fila de resgate (RF-REA-2.5).

        Aceita as duas formas de selecionar da fila: `patients` com os ids
        marcados à mão, ou `filtros` (a mesma querystring da listagem) mais
        `excluir`, que é como a tela diz "o recorte inteiro menos estes".

        Responde com a prestação de contas, e é ela que importa: quem
        selecionou 1.891 pacientes precisa saber que 140 ficaram de fora por
        não ter número e 12 por terem pedido silêncio. Lote que responde só
        "ok" faz a pessoa achar que alcançou todo mundo.
        """
        from apps.patients.api.filtersets import PatientFilterset
        from apps.patients.models import Patient

        sequence = self.get_object()
        ids = request.data.get("patients") or []
        filtros = request.data.get("filtros")

        if filtros is not None:
            # "Marquei o recorte inteiro": são 1.891 pacientes e o navegador só
            # tem os ids da página aberta. Quem expande é o servidor, pelo MESMO
            # filterset que serve a listagem — recalcular o recorte por fora é
            # exatamente como a lista e o contador já divergiram uma vez.
            if not isinstance(filtros, dict):
                raise ValidationError({"filtros": "Formato inválido."})
            recorte = PatientFilterset(
                data=filtros,
                queryset=Patient.objects.filter(clinic=self.clinic),
                request=request,
            ).qs.exclude(pk__in=request.data.get("excluir") or [])
            # A ordem é a da fila (quem sumiu há mais tempo primeiro): quando a
            # seleção não cabe num lote só, entram os mais urgentes, e não um
            # pedaço qualquer.
            recorte = recorte.order_by(F("last_appointment_at").asc(nulls_last=True))
            pacientes = list(recorte[:MAX_POR_LOTE])
            # ⚠️ O que sobrou é DITO, nunca cortado calado: quem marcou 1.891 e
            # recebe "1.000 entraram" sem mais nada acha que alcançou todo mundo.
            sobraram = max(0, recorte.count() - len(pacientes))
            contas = inscrever_em_lote(sequence, pacientes)
            contas["nao_encontrados"] = 0
            contas["fora_do_lote"] = sobraram
            return Response(contas, status=HTTP_201_CREATED)

        if not isinstance(ids, list) or not ids:
            raise ValidationError({"patients": "Informe os pacientes da seleção."})
        if len(ids) > MAX_POR_LOTE:
            raise ValidationError(
                {
                    "patients": (
                        f"A seleção tem {len(ids)} pacientes e o limite por lote é "
                        f"{MAX_POR_LOTE}. Estreite os filtros e faça em partes."
                    )
                }
            )

        pacientes = list(Patient.objects.filter(pk__in=ids, clinic=self.clinic))
        contas = inscrever_em_lote(sequence, pacientes)
        # Selecionado que não existe nesta clínica não some calado.
        contas["nao_encontrados"] = len(set(ids)) - len(pacientes)
        contas["fora_do_lote"] = 0
        return Response(contas, status=HTTP_201_CREATED)

    @action(detail=True, methods=["post"], url_path="unenroll")
    def unenroll(self, request, pk=None):
        """
        Tira da trilha (RF-SEQ-6). Idempotente: sair de onde não se está é 200.

        Aceita `patient` (a ficha, que é quem a recepção tem na mão) **ou**
        `enrollment` (a lista de quem está dentro, que já tem a linha).

        ⚠️ O `enrollment` não é conveniência: quem entra na trilha é o CONTATO,
        e contato sem ficha de paciente não teria como sair por um caminho que
        só entende paciente. A lista mostra essas linhas, então precisa poder
        tirá-las.
        """
        sequence = self.get_object()

        enrollment_id = request.data.get("enrollment")
        if enrollment_id:
            ativa = SequenceEnrollment.objects.filter(
                pk=enrollment_id,
                sequence=sequence,
                status=SequenceEnrollmentStatus.ACTIVE,
            ).first()
        else:
            ativa = SequenceEnrollment.objects.filter(
                sequence=sequence,
                patient=self._paciente(request),
                status=SequenceEnrollmentStatus.ACTIVE,
            ).first()

        if ativa is not None:
            remover(ativa, reason=EnrollmentEndReason.MANUAL)
        return Response({"detail": "Removido da sequência."})

    def _paciente(self, request):
        from apps.patients.models import Patient

        patient_id = request.data.get("patient")
        if not patient_id:
            raise ValidationError({"patient": "Informe o paciente."})
        patient = Patient.objects.filter(pk=patient_id, clinic=self.clinic).first()
        if patient is None:
            raise ValidationError({"patient": "Paciente não encontrado nesta clínica."})
        return patient

    def perform_destroy(self, instance):
        """
        Aposentar a sequência cancela quem está dentro dela (RF-SEQ-6).

        Sem isto as inscrições ficariam apontando para uma trilha que não
        existe mais, vivas para sempre e invisíveis na tela.
        """
        for enrollment in instance.enrollments.filter(
            status=SequenceEnrollmentStatus.ACTIVE
        ):
            remover(enrollment, reason=EnrollmentEndReason.SEQUENCE_RETIRED)
        super().perform_destroy(instance)


class SequenceStepViewSet(AuditMixin, ClinicScopedModelViewSet):
    """
    Passos de uma sequência. Só gestor, como a sequência (RF-SEQ-10).

    Escopo pela clínica vem da sequência-mãe: o passo não é `TenantScopedModel`
    (não tem `clinic` próprio) porque ele não existe fora dela.
    """

    model = SequenceStep
    audit_resource = "SequenceStep"
    serializer_class = SequenceStepSerializer
    permission_classes = [IsClinicManager]
    ordering_fields = ["order"]

    clinic_lookup = "sequence__clinic"

    def get_queryset(self):
        # Ordena pelo RELÓGIO, como o motor (RF-SEQ-2.2).
        return (
            super()
            .get_queryset()
            .select_related("flow", "sequence")
            .order_by("sequence_id", "offset_days", "send_time", "order")
        )

    def perform_create(self, serializer):
        sequence = self._sequencia_da_requisicao()
        # Nasce no fim e a normalização o põe no lugar do relógio dele.
        ultimo = sequence.steps.order_by("-order").values_list("order", flat=True).first()
        passo = serializer.save(
            sequence=sequence,
            order=(ultimo or 0) + 1,
            **self._fluxo_do_aviso(sequence, nome=serializer.validated_data.get("name")),
        )
        normalizar_ordem(sequence)
        return passo

    def perform_update(self, serializer):
        passo = serializer.instance
        serializer.save(
            **self._fluxo_do_aviso(
                passo.sequence,
                nome=serializer.validated_data.get("name") or passo.name,
                atual=passo.flow,
            )
        )
        normalizar_ordem(passo.sequence)

    def _fluxo_do_aviso(self, sequence, *, nome, atual=None) -> dict:
        """
        O atalho do aviso simples (RF-SEQ-1.2).

        Quem monta um lembrete manda `aviso: {template, variables}` no lugar de
        `flow`, e o servidor cria e publica um fluxo de um nó com o nome do
        passo. É o que paga o custo de o passo só disparar fluxo: sem isto a
        clínica abriria o canvas para cada aviso.

        ⚠️ Editar reaproveita o MESMO fluxo quando ele já é um aviso deste
        passo. Criar um novo a cada gravação deixaria um rastro de fluxos
        órfãos publicados, cada um com o nome antigo.
        """
        aviso = self.request.data.get("aviso")
        if not aviso:
            return {}

        titulo = (nome or "Aviso da sequência").strip()
        try:
            flow = criar_fluxo_de_aviso(
                sequence.clinic,
                nome=f"{sequence.name}: {titulo}",
                template_name=(aviso.get("template") or "").strip(),
                variables=aviso.get("variables") or {},
                flow=atual if atual and atual.trigger == FlowTrigger.MANUAL else None,
            )
        except ValueError as erro:
            raise ValidationError({"aviso": erro.args[0]}) from erro
        return {"flow": flow}

    def perform_destroy(self, instance):
        sequence = instance.sequence
        # Antes de apagar: quem está parado NESTE passo segue para o próximo
        # vivo, senão fica apontando para passo morto que a varredura ainda
        # resolveria (RF-SEQ-2.3).
        reapontar_apos_apagar_passo(instance)
        super().perform_destroy(instance)
        normalizar_ordem(sequence)

    def _sequencia_da_requisicao(self):
        sequence_id = self.request.data.get("sequence")
        sequence = Sequence.objects.filter(pk=sequence_id, clinic=self.clinic).first()
        if sequence is None:
            raise ValidationError({"sequence": "Sequência não encontrada nesta clínica."})
        return sequence
