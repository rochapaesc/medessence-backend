"""
A API do painel (RF-SEQ-11): agregado, contagens por passo, próximos disparos
e os motivos de não ter saído.

O painel existe para responder "por que o paciente não recebeu?", então o que
estes testes protegem é a EXPLICAÇÃO, não só os números.
"""

from datetime import time, timedelta

import pytest
from django.utils import timezone

from apps.automation.choices import (
    DispatchSkipReason,
    EnrollmentEndReason,
    EnrollmentSource,
    FlowNodeType,
    FlowStatus,
    HoldReason,
    SequenceDispatchStatus,
    SequenceEnrollmentStatus,
)
from apps.automation.models import (
    Sequence,
    SequenceDispatch,
    SequenceEnrollment,
    SequenceStep,
)
from apps.automation.sequences import inscrever, resolver_disparo
from apps.automation.tests.conftest import make_channel, make_contact, make_flow
from apps.inbox.choices import AttendedBy
from apps.inbox.models import Channel, Conversation
from apps.patients.models import Patient, PatientContact

pytestmark = pytest.mark.django_db

URL = "/api/v1/sequences/"


@pytest.fixture
def trilha(clinic_a):
    """Duas etapas: a véspera e o dia seguinte."""
    sequence = Sequence.objects.create(
        clinic=clinic_a, name="Pós-consulta", is_active=True, is_marketing=False
    )
    flow = make_flow(
        clinic_a,
        name="Aviso",
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                {"id": "n1", "type": FlowNodeType.SEND_TEMPLATE, "config": {"template_name": "t"}}
            ],
            "edges": [],
        },
    )
    for ordem, offset, nome in ((1, -1, "Véspera"), (2, 1, "Dia seguinte")):
        SequenceStep.objects.create(
            sequence=sequence, order=ordem, name=nome, offset_days=offset,
            send_time=time(8, 0), flow=flow,
        )
    return sequence


def paciente(clinic, nome="Ivanita", wa_id="5585900000701"):
    p = Patient.objects.create(clinic=clinic, name=nome)
    PatientContact.objects.create(patient=p, contact=make_contact(clinic, wa_id=wa_id))
    return p


# ---- o agregado da clínica ----


def test_summary_conta_em_trilhas_disparos_e_segurados(
    api_client, manager_single_clinic, clinic_a, trilha
):
    p = paciente(clinic_a)
    contact = p.patient_contacts.first().contact
    inscricao = inscrever(trilha, contact, source=EnrollmentSource.PATIENT_RECORD, patient=p)
    SequenceDispatch.objects.create(
        enrollment=inscricao, step=trilha.steps.first(),
        scheduled_for=timezone.now(), resolved_at=timezone.now(),
        status=SequenceDispatchStatus.STARTED,
    )
    SequenceEnrollment.objects.filter(pk=inscricao.pk).update(
        held_since=timezone.now(), hold_reason=HoldReason.BUSY
    )

    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(f"{URL}summary/")

    assert resposta.status_code == 200
    assert resposta.data == {
        "em_trilhas": 1,
        "disparos_hoje": 1,
        "segurados_agora": 1,
        "responderam": 0,
    }


def test_summary_nao_soma_a_clinica_vizinha(
    api_client, manager_single_clinic, clinic_a, clinic_b, trilha
):
    outra = Sequence.objects.create(clinic=clinic_b, name="Da outra", is_active=True)
    SequenceStep.objects.create(
        sequence=outra, order=1, offset_days=0, send_time=time(8, 0),
        flow=make_flow(clinic_b, name="F", status=FlowStatus.ACTIVE),
    )
    inscrever(outra, make_contact(clinic_b, wa_id="5585900000801"), source=EnrollmentSource.BATCH)

    api_client.force_authenticate(manager_single_clinic)
    assert api_client.get(f"{URL}summary/").data["em_trilhas"] == 0


# ---- contagens por passo ----


def test_detalhe_traz_parados_saidos_e_pulados_por_passo(
    api_client, manager_single_clinic, clinic_a, trilha
):
    vespera, seguinte = trilha.steps.order_by("offset_days")
    p = paciente(clinic_a)
    inscricao = inscrever(
        trilha, p.patient_contacts.first().contact,
        source=EnrollmentSource.PATIENT_RECORD, patient=p,
    )
    SequenceDispatch.objects.create(
        enrollment=inscricao, step=vespera, scheduled_for=timezone.now(),
        resolved_at=timezone.now(), status=SequenceDispatchStatus.STARTED,
    )
    SequenceEnrollment.objects.filter(pk=inscricao.pk).update(current_step=seguinte)

    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(f"{URL}{trilha.pk}/")

    por_nome = {p["name"]: p for p in resposta.data["steps"]}
    assert (por_nome["Véspera"]["saidos"], por_nome["Véspera"]["parados"]) == (1, 0)
    assert (por_nome["Dia seguinte"]["saidos"], por_nome["Dia seguinte"]["parados"]) == (0, 1)


def test_passos_vem_na_ordem_do_relogio(api_client, manager_single_clinic, clinic_a, trilha):
    """A véspera vem primeiro mesmo tendo sido criada com `order` qualquer."""
    api_client.force_authenticate(manager_single_clinic)
    nomes = [p["name"] for p in api_client.get(f"{URL}{trilha.pk}/").data["steps"]]
    assert nomes == ["Véspera", "Dia seguinte"]


# ---- próximos disparos ----


def test_segurados_vem_antes_dos_que_estao_so_na_fila(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    Segurado é o único que pede decisão de gente; o resto sai sozinho na hora.
    """
    na_fila = paciente(clinic_a, nome="Na fila", wa_id="5585900000702")
    preso = paciente(clinic_a, nome="Preso", wa_id="5585900000703")
    for pac in (na_fila, preso):
        inscrever(
            trilha, pac.patient_contacts.first().contact,
            source=EnrollmentSource.PATIENT_RECORD, patient=pac,
        )
    SequenceEnrollment.objects.filter(patient=preso).update(
        held_since=timezone.now() - timedelta(hours=2), hold_reason=HoldReason.BUSY
    )

    api_client.force_authenticate(manager_single_clinic)
    linhas = api_client.get(f"{URL}{trilha.pk}/dispatches/").data

    assert [linha["quem"] for linha in linhas] == ["Preso", "Na fila"]
    assert linhas[0]["hold_reason"] == HoldReason.BUSY
    assert linhas[0]["held_since"] is not None
    assert linhas[1]["hold_reason"] == ""


def test_o_segurado_de_verdade_aparece_com_o_motivo(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """Ponta a ponta: o motor segura e o painel mostra, sem ninguém preencher à mão."""
    p = paciente(clinic_a, nome="Maria", wa_id="5585900000704")
    contact = p.patient_contacts.first().contact
    Conversation.objects.create(
        clinic=clinic_a, channel=make_channel(clinic_a), contact=contact,
        attended_by=AttendedBy.AGENT, last_inbound_at=timezone.now(),
    )
    inscricao = inscrever(
        trilha, contact, source=EnrollmentSource.PATIENT_RECORD, patient=p,
        anchor_at=timezone.now() + timedelta(days=1),
    )
    SequenceEnrollment.objects.filter(pk=inscricao.pk).update(
        next_dispatch_at=timezone.now() - timedelta(minutes=1)
    )
    resolver_disparo(inscricao.pk)

    api_client.force_authenticate(manager_single_clinic)
    linha = api_client.get(f"{URL}{trilha.pk}/dispatches/").data[0]

    assert linha["quem"] == "Maria"
    assert linha["hold_reason"] == HoldReason.BUSY


def test_quem_nao_tem_paciente_aparece_pelo_numero(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """Contato sem paciente vinculado continua sendo alguém na fila."""
    inscrever(
        trilha, make_contact(clinic_a, wa_id="5585900000705"), source=EnrollmentSource.BATCH
    )
    api_client.force_authenticate(manager_single_clinic)
    assert api_client.get(f"{URL}{trilha.pk}/dispatches/").data[0]["quem"]


# ---- o que aconteceu ----


def test_relatorio_agrupa_pulos_e_saidas_por_motivo(
    api_client, manager_single_clinic, clinic_a, trilha
):
    vespera = trilha.steps.order_by("offset_days").first()
    for i, motivo in enumerate(
        [DispatchSkipReason.NO_WINDOW, DispatchSkipReason.NO_WINDOW, DispatchSkipReason.EXPIRED]
    ):
        p = paciente(clinic_a, nome=f"P{i}", wa_id=f"5585900000{810 + i}")
        inscricao = inscrever(
            trilha, p.patient_contacts.first().contact,
            source=EnrollmentSource.PATIENT_RECORD, patient=p,
        )
        SequenceDispatch.objects.create(
            enrollment=inscricao, step=vespera, scheduled_for=timezone.now(),
            resolved_at=timezone.now(), status=SequenceDispatchStatus.SKIPPED,
            skip_reason=motivo,
        )

    saiu = paciente(clinic_a, nome="Saiu", wa_id="5585900000820")
    inscricao = inscrever(
        trilha, saiu.patient_contacts.first().contact,
        source=EnrollmentSource.PATIENT_RECORD, patient=saiu,
    )
    inscricao.status = SequenceEnrollmentStatus.CANCELED
    inscricao.end_reason = EnrollmentEndReason.OPTED_OUT
    inscricao.save(update_fields=["status", "end_reason"])

    api_client.force_authenticate(manager_single_clinic)
    relatorio = api_client.get(f"{URL}{trilha.pk}/report/").data

    assert relatorio["dias"] == 30
    assert relatorio["pulos"][0] == {"motivo": DispatchSkipReason.NO_WINDOW, "total": 2}
    assert {"motivo": DispatchSkipReason.EXPIRED, "total": 1} in relatorio["pulos"]
    assert relatorio["saidas"] == [{"motivo": EnrollmentEndReason.OPTED_OUT, "total": 1}]


def test_trilha_sem_historico_devolve_listas_vazias(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """Tela nova de clínica nova não pode quebrar por falta de dado."""
    api_client.force_authenticate(manager_single_clinic)
    relatorio = api_client.get(f"{URL}{trilha.pk}/report/").data

    assert relatorio["pulos"] == []
    assert relatorio["saidas"] == []


# ---- papéis ----


def test_atendente_ve_o_painel(api_client, attendant_a, clinic_a, trilha):
    """
    Quem atende precisa ver por que um paciente não recebeu, senão a explicação
    fica só com quem gerencia e a recepção continua no escuro.
    """
    api_client.force_authenticate(attendant_a)
    assert api_client.get(f"{URL}summary/").status_code == 200
    assert api_client.get(f"{URL}{trilha.pk}/dispatches/").status_code == 200
    assert api_client.get(f"{URL}{trilha.pk}/report/").status_code == 200


# ---- resultado por passo (RF-SEQ-11.3) ----


def _disparo_com_execucao(clinic, trilha, step, pac, *, status_da_fala="", respondeu=False):
    """Um disparo completo: inscrição, execução, a fala e o que houve com ela."""
    from apps.automation.choices import FlowRunEventType, FlowRunStatus
    from apps.automation.models import FlowRun, FlowRunEvent
    from apps.inbox.choices import MessageKind, SenderKind
    from apps.inbox.models import Message

    contact = pac.patient_contacts.first().contact
    inscricao = inscrever(
        trilha, contact, source=EnrollmentSource.PATIENT_RECORD, patient=pac
    )
    # Um canal por clínica é unicidade do modelo: reusa o que existir.
    canal = Channel.objects.filter(clinic=clinic).first() or make_channel(clinic)
    conversa = Conversation.objects.create(
        clinic=clinic, channel=canal, contact=contact
    )
    run = FlowRun.objects.create(
        clinic=clinic,
        flow=step.flow,
        version=step.flow.current_version,
        contact=contact,
        conversation=conversa,
        current_node="n1",
        status=FlowRunStatus.COMPLETED,
        last_advanced_at=timezone.now(),
    )
    # DUAS falas, como uma execução real: o modelo e depois os botões. Só a
    # PRIMEIRA representa o disparo (RF-SEQ-11.3).
    for i, kind in enumerate([MessageKind.TEMPLATE, MessageKind.INTERACTIVE]):
        msg = Message.objects.create(
            clinic=clinic,
            conversation=conversa,
            kind=kind,
            body="oi",
            sender_kind=SenderKind.BOT,
            status=status_da_fala if i == 0 else "",
            wa_timestamp=timezone.now(),
        )
        FlowRunEvent.objects.create(
            run=run, node_key="n1", event_type=FlowRunEventType.SENT,
            data={"message_id": msg.pk},
        )
    if respondeu:
        FlowRunEvent.objects.create(
            run=run, node_key="n1", event_type=FlowRunEventType.REPLIED, data={}
        )

    return SequenceDispatch.objects.create(
        enrollment=inscricao, step=step, scheduled_for=timezone.now(),
        resolved_at=timezone.now(), status=SequenceDispatchStatus.STARTED,
        flow_run=run,
    )


def test_resultado_conta_por_disparo_e_nao_por_mensagem(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    ⚠️ Uma execução manda DUAS falas. Contar mensagens daria dois entregues
    para um paciente só, e o número deixaria de ser comparável com os disparos.
    """
    from apps.inbox.choices import MessageStatus

    vespera = trilha.steps.order_by("offset_days").first()
    p = paciente(clinic_a, nome="Lida", wa_id="5585900000901")
    _disparo_com_execucao(
        clinic_a, trilha, vespera, p, status_da_fala=MessageStatus.READ, respondeu=True
    )

    api_client.force_authenticate(manager_single_clinic)
    passos = api_client.get(f"{URL}{trilha.pk}/results/").data["passos"]

    assert len(passos) == 1
    assert passos[0] == {
        "step": vespera.pk,
        "disparos": 1,
        "entregues": 1,
        "lidas": 1,
        "responderam": 1,
        "agendaram": 0,
    }


def test_lida_implica_entregue_e_enviada_nao(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """A escala do status é progressiva (§9.5): quem leu recebeu."""
    from apps.inbox.choices import MessageStatus

    vespera = trilha.steps.order_by("offset_days").first()
    for i, status in enumerate(
        [MessageStatus.SENT, MessageStatus.DELIVERED, MessageStatus.READ]
    ):
        p = paciente(clinic_a, nome=f"P{i}", wa_id=f"5585900000{910 + i}")
        _disparo_com_execucao(clinic_a, trilha, vespera, p, status_da_fala=status)

    api_client.force_authenticate(manager_single_clinic)
    passo = api_client.get(f"{URL}{trilha.pk}/results/").data["passos"][0]

    assert passo["disparos"] == 3
    assert passo["entregues"] == 2, "enviada ainda não é entregue"
    assert passo["lidas"] == 1


def test_responder_tres_vezes_conta_um_paciente(
    api_client, manager_single_clinic, clinic_a, trilha
):
    from apps.automation.choices import FlowRunEventType
    from apps.automation.models import FlowRunEvent

    vespera = trilha.steps.order_by("offset_days").first()
    p = paciente(clinic_a, nome="Falante", wa_id="5585900000920")
    disparo = _disparo_com_execucao(clinic_a, trilha, vespera, p, respondeu=True)
    for _ in range(2):
        FlowRunEvent.objects.create(
            run=disparo.flow_run, node_key="n1",
            event_type=FlowRunEventType.REPLIED, data={},
        )

    api_client.force_authenticate(manager_single_clinic)
    assert api_client.get(f"{URL}{trilha.pk}/results/").data["passos"][0]["responderam"] == 1


def test_agendou_respeita_a_janela_da_sequencia(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    RF-SEQ-11.2: a janela é POR SEQUÊNCIA. Consulta marcada depois dela não é
    resultado desta trilha, e creditar seria inflar o número com o que veio de
    outra coisa.
    """
    from apps.scheduling.models import Appointment, Practitioner

    trilha.conversion_days = 7
    trilha.save(update_fields=["conversion_days"])
    vespera = trilha.steps.order_by("offset_days").first()
    profissional = Practitioner.objects.create(clinic=clinic_a, name="Dra. Alfa")

    dentro = paciente(clinic_a, nome="Marcou logo", wa_id="5585900000930")
    fora = paciente(clinic_a, nome="Marcou tarde", wa_id="5585900000931")
    d1 = _disparo_com_execucao(clinic_a, trilha, vespera, dentro)
    d2 = _disparo_com_execucao(clinic_a, trilha, vespera, fora)

    for pac, dias, disparo in ((dentro, 2, d1), (fora, 20, d2)):
        consulta = Appointment.objects.create(
            clinic=clinic_a, patient=pac, practitioner=profissional,
            starts_at=timezone.now() + timedelta(days=40),
        )
        Appointment.objects.filter(pk=consulta.pk).update(
            created_at=disparo.resolved_at + timedelta(days=dias)
        )

    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(f"{URL}{trilha.pk}/results/").data

    assert resposta["janela_do_agendou"] == 7
    assert resposta["passos"][0]["agendaram"] == 1


def test_trilha_sem_disparo_devolve_lista_vazia(
    api_client, manager_single_clinic, clinic_a, trilha
):
    api_client.force_authenticate(manager_single_clinic)
    assert api_client.get(f"{URL}{trilha.pk}/results/").data["passos"] == []


def test_agregado_conta_respostas_por_execucao(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    O número do topo é de EXECUÇÕES com resposta, não de mensagens recebidas:
    responder três vezes é um paciente que respondeu.
    """
    from apps.automation.choices import FlowRunEventType
    from apps.automation.models import FlowRunEvent

    vespera = trilha.steps.order_by("offset_days").first()
    p = paciente(clinic_a, nome="Respondeu", wa_id="5585900000940")
    disparo = _disparo_com_execucao(clinic_a, trilha, vespera, p, respondeu=True)
    FlowRunEvent.objects.create(
        run=disparo.flow_run, node_key="n1",
        event_type=FlowRunEventType.REPLIED, data={},
    )
    # Outro que recebeu e não respondeu.
    _disparo_com_execucao(
        clinic_a, trilha, vespera,
        paciente(clinic_a, nome="Calado", wa_id="5585900000941"),
    )

    api_client.force_authenticate(manager_single_clinic)
    assert api_client.get(f"{URL}summary/").data["responderam"] == 1


def test_resposta_depois_da_execucao_terminar_conta(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    ⚠️ O caso da CAMPANHA (18/08, achado ao vivo com o WILLIAN): fluxo de um
    nó termina no instante do envio, a resposta chega minutos depois, e o
    evento `replied` nunca nasce. Sem esta conta, o CTR de toda campanha
    leria zero para sempre.
    """
    from apps.inbox.choices import MessageKind, SenderKind
    from apps.inbox.models import Message

    vespera = trilha.steps.order_by("offset_days").first()
    p = paciente(clinic_a, nome="Respondeu depois", wa_id="5585900000930")
    disparo = _disparo_com_execucao(clinic_a, trilha, vespera, p, respondeu=False)

    # A resposta chega DEPOIS, na conversa, sem execução ativa nenhuma.
    Message.objects.create(
        clinic=clinic_a,
        conversation=disparo.flow_run.conversation,
        kind=MessageKind.TEXT,
        body="Oi! Quero sim.",
        sender_kind=SenderKind.CONTACT,
        wa_timestamp=timezone.now(),
    )

    api_client.force_authenticate(manager_single_clinic)
    passo = api_client.get(f"{URL}{trilha.pk}/results/").data["passos"][0]

    assert passo["responderam"] == 1


def test_resposta_ao_passo_seguinte_nao_conta_para_o_anterior(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """O corte de atribuição: cada disparo responde até o disparo seguinte."""
    from datetime import timedelta

    from apps.automation.models import SequenceDispatch
    from apps.inbox.choices import MessageKind, SenderKind
    from apps.inbox.models import Message

    passos = list(trilha.steps.order_by("offset_days"))
    p = paciente(clinic_a, nome="Respondeu tarde", wa_id="5585900000931")
    primeiro = _disparo_com_execucao(clinic_a, trilha, passos[0], p)

    # O disparo do passo seguinte, na MESMA conversa, um dia depois.
    agora = timezone.now()
    SequenceDispatch.objects.filter(pk=primeiro.pk).update(
        resolved_at=agora - timedelta(days=1)
    )
    segundo = _disparo_com_execucao_na_conversa(
        clinic_a, trilha, passos[1], primeiro
    )

    # A resposta chega DEPOIS do segundo disparo: é dele, não do primeiro.
    Message.objects.create(
        clinic=clinic_a,
        conversation=primeiro.flow_run.conversation,
        kind=MessageKind.TEXT,
        body="respondi",
        sender_kind=SenderKind.CONTACT,
        wa_timestamp=timezone.now(),
    )

    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(f"{URL}{trilha.pk}/results/").data["passos"]
    por_passo = {linha["step"]: linha for linha in resposta}

    assert por_passo[passos[0].pk]["responderam"] == 0, "a resposta é do passo 2"
    assert por_passo[passos[1].pk]["responderam"] == 1


def _disparo_com_execucao_na_conversa(clinic, trilha, step, disparo_anterior):
    """Um segundo disparo da MESMA inscrição, na mesma conversa."""
    from apps.automation.choices import FlowRunStatus, SequenceDispatchStatus
    from apps.automation.models import FlowRun, SequenceDispatch

    run = FlowRun.objects.create(
        clinic=clinic,
        flow=step.flow,
        version=step.flow.current_version,
        contact=disparo_anterior.enrollment.contact,
        conversation=disparo_anterior.flow_run.conversation,
        current_node="n1",
        status=FlowRunStatus.COMPLETED,
        last_advanced_at=timezone.now(),
    )
    return SequenceDispatch.objects.create(
        enrollment=disparo_anterior.enrollment,
        step=step,
        scheduled_for=timezone.now(),
        resolved_at=timezone.now(),
        status=SequenceDispatchStatus.STARTED,
        flow_run=run,
    )
