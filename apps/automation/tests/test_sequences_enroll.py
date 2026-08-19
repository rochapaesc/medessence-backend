"""
As portas de inscrição, as travas e o calendário (RF-SEQ-2/3/4/6).

A âncora é o que faz as quatro portas caberem num modelo só: nas de gente ela
é o instante da inscrição, e na de consulta é a data da consulta.
"""

from datetime import time, timedelta

import pytest
from django.utils import timezone

from apps.automation.choices import (
    EnrollmentEndReason,
    EnrollmentSource,
    FlowNodeType,
    FlowStatus,
    SequenceEnrollmentStatus,
)
from apps.automation.models import Sequence, SequenceEnrollment, SequenceStep
from apps.automation.sequences import (
    SemContato,
    contato_do_paciente,
    inscrever,
    recalcular,
    remover,
)
from apps.automation.tests.conftest import make_channel, make_contact, make_flow
from apps.inbox.models import Conversation
from apps.patients.models import Patient, PatientContact

pytestmark = pytest.mark.django_db


def trilha(clinic, *, offsets=(0,), marketing=True):
    sequence = Sequence.objects.create(
        clinic=clinic, name=f"Trilha {timezone.now().timestamp()}",
        is_active=True, is_marketing=marketing,
    )
    flow = make_flow(clinic, name=f"F{timezone.now().timestamp()}", status=FlowStatus.ACTIVE)
    for ordem, offset in enumerate(offsets, start=1):
        SequenceStep.objects.create(
            sequence=sequence,
            order=ordem,
            offset_days=offset,
            send_time=time(8, 0),
            flow=flow,
        )
    return sequence


# ---- travas ----


def test_um_contato_nao_entra_duas_vezes_na_mesma_trilha(clinic_a):
    contact = make_contact(clinic_a)
    sequence = trilha(clinic_a)

    primeiro = inscrever(sequence, contact, source=EnrollmentSource.FLOW_NODE)
    segundo = inscrever(sequence, contact, source=EnrollmentSource.PATIENT_RECORD)

    assert primeiro is not None
    # No-op de propósito: a trava é do banco, e quem chegou primeiro vale.
    assert segundo is None
    assert SequenceEnrollment.objects.filter(sequence=sequence).count() == 1


def test_duas_consultas_futuras_sao_duas_inscricoes_legitimas(clinic_a):
    """
    A trava por contato NÃO vale para as ancoradas em consulta: duas consultas
    futuras do mesmo paciente são duas jornadas, cada uma com sua âncora.
    """
    from apps.scheduling.models import Appointment, Practitioner

    contact = make_contact(clinic_a)
    patient = Patient.objects.create(clinic=clinic_a, name="Ivanita")
    profissional = Practitioner.objects.create(clinic=clinic_a, name="Dra. Alfa")
    sequence = trilha(clinic_a)

    consultas = [
        Appointment.objects.create(
            clinic=clinic_a,
            patient=patient,
            practitioner=profissional,
            starts_at=timezone.now() + timedelta(days=dias),
        )
        for dias in (3, 20)
    ]
    for consulta in consultas:
        assert (
            inscrever(
                sequence,
                contact,
                source=EnrollmentSource.APPOINTMENT,
                patient=patient,
                appointment=consulta,
                anchor_at=consulta.starts_at,
            )
            is not None
        )

    assert SequenceEnrollment.objects.filter(sequence=sequence).count() == 2


def test_contato_com_opt_out_nao_entra_em_trilha_de_marketing(clinic_a):
    contact = make_contact(clinic_a)
    contact.marketing_opt_out = True
    contact.save(update_fields=["marketing_opt_out"])

    assert inscrever(trilha(clinic_a), contact, source=EnrollmentSource.BATCH) is None
    # A operacional entra: parar promoção não é parar de confirmar consulta.
    assert (
        inscrever(trilha(clinic_a, marketing=False), contact, source=EnrollmentSource.BATCH)
        is not None
    )


def test_trilha_sem_passo_ativo_nao_inscreve(clinic_a):
    sequence = Sequence.objects.create(clinic=clinic_a, name="Vazia", is_active=True)
    assert inscrever(sequence, make_contact(clinic_a), source=EnrollmentSource.BATCH) is None


# ---- calendário ----


def test_a_ancora_da_consulta_faz_o_passo_negativo_cair_na_vespera(clinic_a):
    """É por isso que o prazo conta da âncora e não do passo anterior."""
    contact = make_contact(clinic_a)
    sequence = trilha(clinic_a, offsets=(-1,))
    consulta_em = timezone.now() + timedelta(days=10)

    enrollment = inscrever(
        sequence,
        contact,
        source=EnrollmentSource.APPOINTMENT,
        anchor_at=consulta_em,
    )

    assert enrollment.next_dispatch_at < consulta_em
    assert (consulta_em - enrollment.next_dispatch_at) < timedelta(days=2)


def test_reancorar_recalcula_o_que_ainda_nao_saiu(clinic_a):
    contact = make_contact(clinic_a)
    sequence = trilha(clinic_a, offsets=(-1,))
    antes = timezone.now() + timedelta(days=10)
    enrollment = inscrever(
        sequence, contact, source=EnrollmentSource.APPOINTMENT, anchor_at=antes
    )
    primeiro_disparo = enrollment.next_dispatch_at

    recalcular(enrollment, anchor_at=antes + timedelta(days=7))
    enrollment.refresh_from_db()

    assert enrollment.next_dispatch_at > primeiro_disparo


def test_lote_desliza_os_disparos_em_vez_de_soltar_todos_juntos(clinic_a):
    """RF-SEQ-9: o primeiro passo de um lote grande não sai todo no mesmo minuto."""
    sequence = trilha(clinic_a)
    inscricoes = [
        inscrever(
            sequence,
            make_contact(clinic_a, wa_id=f"55859000001{i:02d}"),
            source=EnrollmentSource.BATCH,
            atraso=timedelta(minutes=i),
        )
        for i in range(3)
    ]
    horarios = [i.next_dispatch_at for i in inscricoes]
    assert len(set(horarios)) == 3


# ---- saídas ----


def test_remover_e_idempotente(clinic_a):
    enrollment = inscrever(
        trilha(clinic_a), make_contact(clinic_a), source=EnrollmentSource.PATIENT_RECORD
    )
    remover(enrollment, reason=EnrollmentEndReason.MANUAL)
    remover(enrollment, reason=EnrollmentEndReason.FLOW_NODE)

    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.CANCELED
    # O primeiro motivo é o que vale: quem saiu, saiu.
    assert enrollment.end_reason == EnrollmentEndReason.MANUAL
    assert enrollment.next_dispatch_at is None


# ---- por qual número a trilha fala ----


def test_paciente_sem_numero_nao_entra_e_o_erro_diz_por_que(clinic_a):
    patient = Patient.objects.create(clinic=clinic_a, name="Sem WhatsApp")
    with pytest.raises(SemContato):
        contato_do_paciente(patient)


def test_com_mais_de_um_numero_vence_o_da_conversa_mais_recente(clinic_a):
    patient = Patient.objects.create(clinic=clinic_a, name="Dois números")
    antigo = make_contact(clinic_a, wa_id="5585900000011")
    novo = make_contact(clinic_a, wa_id="5585900000012")
    for contact in (antigo, novo):
        PatientContact.objects.create(patient=patient, contact=contact)

    canal = make_channel(clinic_a)
    Conversation.objects.create(
        clinic=clinic_a, channel=canal, contact=antigo,
        last_message_at=timezone.now() - timedelta(days=5),
    )
    Conversation.objects.create(
        clinic=clinic_a, channel=canal, contact=novo, last_message_at=timezone.now()
    )

    assert contato_do_paciente(patient) == novo


# ---- os nós de fluxo ----


def test_no_de_fluxo_inscreve_e_remove(clinic_a):
    from apps.automation.engine import start_run
    from apps.inbox.choices import AttendedBy

    contact = make_contact(clinic_a)
    sequence = trilha(clinic_a)
    conversa = Conversation.objects.create(
        clinic=clinic_a,
        channel=make_channel(clinic_a),
        contact=contact,
        attended_by=AttendedBy.NONE,
        last_inbound_at=timezone.now(),
    )

    inscritor = make_flow(
        clinic_a,
        name="Inscreve",
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                {
                    "id": "n1",
                    "type": FlowNodeType.ENROLL_SEQUENCE,
                    "config": {"sequence_id": sequence.pk},
                }
            ],
            "edges": [],
        },
    )
    start_run(inscritor, conversa)
    assert SequenceEnrollment.objects.filter(
        sequence=sequence, contact=contact, status=SequenceEnrollmentStatus.ACTIVE
    ).exists()

    conversa.refresh_from_db()
    conversa.attended_by = AttendedBy.NONE
    conversa.save(update_fields=["attended_by"])

    removedor = make_flow(
        clinic_a,
        name="Remove",
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                {
                    "id": "n1",
                    "type": FlowNodeType.UNENROLL_SEQUENCE,
                    "config": {"sequence_id": sequence.pk},
                }
            ],
            "edges": [],
        },
    )
    start_run(removedor, conversa)

    enrollment = SequenceEnrollment.objects.get(sequence=sequence, contact=contact)
    assert enrollment.status == SequenceEnrollmentStatus.CANCELED
    assert enrollment.end_reason == EnrollmentEndReason.FLOW_NODE


def test_no_que_aponta_para_sequencia_apagada_nao_derruba_o_fluxo(clinic_a):
    """
    RF-FLW-21: a conversa do paciente vale mais do que a inscrição na trilha.
    Exceção aqui derrubaria o avanço inteiro.
    """
    from apps.automation.engine import start_run
    from apps.inbox.choices import AttendedBy

    contact = make_contact(clinic_a)
    conversa = Conversation.objects.create(
        clinic=clinic_a,
        channel=make_channel(clinic_a),
        contact=contact,
        attended_by=AttendedBy.NONE,
        last_inbound_at=timezone.now(),
    )
    flow = make_flow(
        clinic_a,
        name="Aponta para o nada",
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                {
                    "id": "n1",
                    "type": FlowNodeType.ENROLL_SEQUENCE,
                    "config": {"sequence_id": 999999},
                }
            ],
            "edges": [],
        },
    )

    assert start_run(flow, conversa) is not None


# ---- por qual NÚMERO a trilha fala (RF-SEQ-3.6, corrigido em 18/08/2026) ----


def _com_dois_numeros(clinic):
    """
    Um paciente com dois números, como acontece de verdade: o dele e o de quem
    divide a ficha (RF-PAC-7). O SEGUNDO é o que conversou por último.
    """
    from apps.inbox.models import Conversation
    from apps.patients.models import Patient, PatientContact

    from apps.automation.tests.conftest import make_channel, make_contact

    canal = make_channel(clinic)
    patient = Patient.objects.create(clinic=clinic, name="Willian de teste")

    principal = make_contact(clinic, wa_id="5589900000001")
    outro = make_contact(clinic, wa_id="5589900000002")
    PatientContact.objects.create(patient=patient, contact=principal, is_primary=True)
    PatientContact.objects.create(patient=patient, contact=outro, is_primary=False)

    agora = timezone.now()
    Conversation.objects.create(
        clinic=clinic,
        channel=canal,
        contact=principal,
        last_message_at=agora - timedelta(hours=9),
    )
    Conversation.objects.create(
        clinic=clinic,
        channel=canal,
        contact=outro,
        last_message_at=agora,  # falou AGORA, e mesmo assim não deve vencer
    )
    return patient, principal, outro


def test_o_principal_da_ficha_vence_a_conversa_mais_recente(clinic_a):
    """
    ⚠️ Defeito real de 18/08: a trilha foi para o número de OUTRA pessoa da
    mesma ficha, só porque alguém tinha conversado por ali mais recentemente.
    O dono do produto ficou olhando a conversa certa, vazia.
    """
    from apps.automation.sequences import contato_do_paciente

    patient, principal, _outro = _com_dois_numeros(clinic_a)

    assert contato_do_paciente(patient).pk == principal.pk


def test_o_lote_escolhe_o_mesmo_numero_que_a_ficha(clinic_a):
    """
    Se as duas regras divergirem, inscrever pela ficha e inscrever por um lote
    de mil mandam a mensagem para números diferentes - e isso só aparece com o
    paciente do outro lado.
    """
    from apps.automation.sequences import contatos_dos_pacientes, contato_do_paciente

    patient, _principal, _outro = _com_dois_numeros(clinic_a)

    do_lote = contatos_dos_pacientes([patient])[patient.pk]
    assert do_lote.pk == contato_do_paciente(patient).pk


def test_sem_principal_marcado_vale_quem_falou_por_ultimo(clinic_a):
    """A regra antiga continua valendo como desempate, e só como desempate."""
    from apps.inbox.models import Conversation
    from apps.patients.models import Patient, PatientContact

    from apps.automation.sequences import contato_do_paciente
    from apps.automation.tests.conftest import make_channel, make_contact

    canal = make_channel(clinic_a)
    patient = Patient.objects.create(clinic=clinic_a, name="Sem principal")
    antigo = make_contact(clinic_a, wa_id="5589900000003")
    recente = make_contact(clinic_a, wa_id="5589900000004")
    PatientContact.objects.create(patient=patient, contact=antigo, is_primary=False)
    PatientContact.objects.create(patient=patient, contact=recente, is_primary=False)

    agora = timezone.now()
    Conversation.objects.create(
        clinic=clinic_a, channel=canal, contact=antigo, last_message_at=agora - timedelta(days=2)
    )
    Conversation.objects.create(
        clinic=clinic_a, channel=canal, contact=recente, last_message_at=agora
    )

    assert contato_do_paciente(patient).pk == recente.pk
