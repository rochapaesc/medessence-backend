"""
A porta automática da consulta (RF-SEQ-3.4): inscrever, reancorar e cancelar.

O sinal é UM só para os dois caminhos de origem, a consulta marcada aqui e a
espelhada da vSaúde, porque o pull grava por `update_or_create`. Estes testes
cobrem os dois, e cobrem as guardas que tornam o backfill inofensivo.
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
from apps.automation.tests.conftest import make_channel, make_contact, make_flow
from apps.patients.models import Patient, PatientContact
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import Appointment, Practitioner

pytestmark = pytest.mark.django_db


@pytest.fixture
def trilha_da_consulta(clinic_a):
    """D-1 (véspera) e D+1 (dia seguinte): a jornada clássica de consulta."""
    sequence = Sequence.objects.create(
        clinic=clinic_a,
        name="Jornada da consulta",
        is_active=True,
        is_marketing=False,
        enroll_on_appointment=True,
    )
    # Grafo de verdade: fluxo sem nó de entrada é pulado antes de qualquer
    # regra de consulta (é a guarda do `_tem_no_de_entrada`), e o teste
    # passaria a medir a coisa errada.
    flow = make_flow(
        clinic_a,
        name="Aviso da consulta",
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                {
                    "id": "n1",
                    "type": FlowNodeType.SEND_TEMPLATE,
                    "config": {"template_name": "aviso"},
                }
            ],
            "edges": [],
        },
    )
    for ordem, offset in ((1, -1), (2, 1)):
        SequenceStep.objects.create(
            sequence=sequence, order=ordem, offset_days=offset, send_time=time(8, 0), flow=flow
        )
    return sequence


@pytest.fixture
def paciente(clinic_a):
    patient = Patient.objects.create(clinic=clinic_a, name="Ivanita")
    make_channel(clinic_a)
    PatientContact.objects.create(patient=patient, contact=make_contact(clinic_a))
    return patient


@pytest.fixture
def profissional(clinic_a):
    return Practitioner.objects.create(clinic=clinic_a, name="Dra. Alfa")


def marcar(clinic, patient, profissional, *, daqui=timedelta(days=10), **extra):
    return Appointment.objects.create(
        clinic=clinic,
        patient=patient,
        practitioner=profissional,
        starts_at=timezone.now() + daqui,
        **extra,
    )


# ---- inscrever ----


def test_consulta_futura_inscreve_ancorada_nela(
    clinic_a, trilha_da_consulta, paciente, profissional
):
    consulta = marcar(clinic_a, paciente, profissional)

    enrollment = SequenceEnrollment.objects.get(appointment=consulta)
    assert enrollment.source == EnrollmentSource.APPOINTMENT
    assert enrollment.anchor_at == consulta.starts_at
    # Primeiro passo é o D-1: cai ANTES da consulta.
    assert enrollment.next_dispatch_at < consulta.starts_at


def test_consulta_passada_nao_inscreve(clinic_a, trilha_da_consulta, paciente, profissional):
    """
    A guarda que torna o backfill barato e inofensivo: espelhar 10 mil
    consultas antigas não pode inscrever ninguém nem despejar confirmação de
    véspera fora de hora.
    """
    marcar(clinic_a, paciente, profissional, daqui=timedelta(days=-3))

    assert not SequenceEnrollment.objects.exists()


def test_consulta_ja_cancelada_nao_inscreve(clinic_a, trilha_da_consulta, paciente, profissional):
    marcar(clinic_a, paciente, profissional, status=AppointmentStatus.CANCELED)

    assert not SequenceEnrollment.objects.exists()


def test_sequencia_que_nao_pede_consulta_fica_de_fora(
    clinic_a, trilha_da_consulta, paciente, profissional
):
    trilha_da_consulta.enroll_on_appointment = False
    trilha_da_consulta.save(update_fields=["enroll_on_appointment"])

    marcar(clinic_a, paciente, profissional)
    assert not SequenceEnrollment.objects.exists()


def test_paciente_sem_numero_nao_derruba_o_save(
    clinic_a, trilha_da_consulta, profissional
):
    """
    Cadastro incompleto não é erro de sincronização: a consulta grava, e a
    inscrição simplesmente não acontece.
    """
    sem_numero = Patient.objects.create(clinic=clinic_a, name="Sem WhatsApp")

    consulta = marcar(clinic_a, sem_numero, profissional)

    assert consulta.pk is not None
    assert not SequenceEnrollment.objects.exists()


def test_salvar_de_novo_nao_inscreve_duas_vezes(
    clinic_a, trilha_da_consulta, paciente, profissional
):
    consulta = marcar(clinic_a, paciente, profissional)
    consulta.comments_html = "<p>mexeu em outra coisa</p>"
    consulta.save()

    assert SequenceEnrollment.objects.filter(appointment=consulta).count() == 1


# ---- reancorar ----


def test_mudar_a_data_recalcula_o_calendario_pendente(
    clinic_a, trilha_da_consulta, paciente, profissional
):
    consulta = marcar(clinic_a, paciente, profissional)
    antes = SequenceEnrollment.objects.get(appointment=consulta).next_dispatch_at

    consulta.starts_at = consulta.starts_at + timedelta(days=7)
    consulta.save()

    enrollment = SequenceEnrollment.objects.get(appointment=consulta)
    assert enrollment.anchor_at == consulta.starts_at
    assert enrollment.next_dispatch_at > antes


def test_remarcar_duplicando_gera_duas_jornadas(
    clinic_a, trilha_da_consulta, paciente, profissional
):
    """
    "Remarcar DUPLICA" é invariante da agenda: a consulta nova é outra linha, e
    cada uma tem a própria âncora. A original segue a regra do próprio status.
    """
    original = marcar(clinic_a, paciente, profissional)
    duplicada = marcar(clinic_a, paciente, profissional, daqui=timedelta(days=20))

    assert SequenceEnrollment.objects.count() == 2
    ancoras = set(
        SequenceEnrollment.objects.values_list("anchor_at", flat=True)
    )
    assert ancoras == {original.starts_at, duplicada.starts_at}


# ---- cancelar ----


def test_cancelar_a_consulta_cancela_a_inscricao(
    clinic_a, trilha_da_consulta, paciente, profissional
):
    consulta = marcar(clinic_a, paciente, profissional)
    consulta.status = AppointmentStatus.CANCELED
    consulta.save()

    enrollment = SequenceEnrollment.objects.get(appointment=consulta)
    assert enrollment.status == SequenceEnrollmentStatus.CANCELED
    assert enrollment.end_reason == EnrollmentEndReason.APPOINTMENT_CANCELED
    assert enrollment.next_dispatch_at is None


def test_apagar_a_consulta_cancela_a_inscricao(
    clinic_a, trilha_da_consulta, paciente, profissional
):
    """O delete do projeto é SOFT, então passa pelo mesmo `post_save`."""
    consulta = marcar(clinic_a, paciente, profissional)
    consulta.delete()

    enrollment = SequenceEnrollment.objects.get(appointment=consulta)
    assert enrollment.status == SequenceEnrollmentStatus.CANCELED
    assert enrollment.end_reason == EnrollmentEndReason.APPOINTMENT_CANCELED


def test_consulta_realizada_mantem_a_jornada_viva(
    clinic_a, trilha_da_consulta, paciente, profissional
):
    """
    O passo D+1 existe justamente para depois do atendimento: marcar como
    realizada NÃO pode encerrar a trilha.
    """
    consulta = marcar(clinic_a, paciente, profissional)
    consulta.status = AppointmentStatus.COMPLETED
    consulta.save()

    enrollment = SequenceEnrollment.objects.get(appointment=consulta)
    assert enrollment.status == SequenceEnrollmentStatus.ACTIVE


# ---- as regras do passo pós-consulta (RF-SEQ-7.0/7.1) ----


def _passo_pos_consulta_vencido(clinic, consulta, *, offset=1, hora=time(18, 0)):
    """Deixa a inscrição parada num passo que cai DEPOIS da consulta, e vencido."""
    from apps.automation.sequences import horario_do_passo

    enrollment = SequenceEnrollment.objects.get(appointment=consulta)
    passo = enrollment.sequence.steps.get(order=2)
    passo.offset_days = offset
    passo.send_time = hora
    passo.expire_hours = 240
    passo.save()

    enrollment.current_step = passo
    enrollment.next_dispatch_at = horario_do_passo(passo, enrollment.anchor_at, clinic)
    enrollment.save(update_fields=["current_step", "next_dispatch_at"])
    SequenceEnrollment.objects.filter(pk=enrollment.pk).update(
        next_dispatch_at=timezone.now() - timedelta(minutes=1)
    )
    return enrollment


def test_quem_faltou_nao_recebe_o_passo_de_depois(
    clinic_a, trilha_da_consulta, paciente, profissional
):
    """
    RF-SEQ-7.1: 'como foi o seu atendimento?' para quem não apareceu é pior do
    que silêncio. Mas a falta NÃO encerra a trilha, diferente do cancelamento.
    """
    from apps.automation.choices import DispatchSkipReason
    from apps.automation.sequences import resolver_disparo

    consulta = marcar(clinic_a, paciente, profissional, daqui=timedelta(days=-2))
    # A consulta nasceu no passado, então inscrevo pela porta de gente e
    # ancoro nela, que é o estado real de quem faltou ontem.
    from apps.automation.choices import EnrollmentSource as Fonte
    from apps.automation.sequences import inscrever

    inscrever(
        trilha_da_consulta,
        paciente.patient_contacts.first().contact,
        source=Fonte.APPOINTMENT,
        patient=paciente,
        appointment=consulta,
        anchor_at=consulta.starts_at,
    )
    consulta.status = AppointmentStatus.NO_SHOW
    consulta.save()

    enrollment = _passo_pos_consulta_vencido(clinic_a, consulta)
    assert resolver_disparo(enrollment.pk) == "pulado_paciente_faltou"

    disparo = enrollment.dispatches.get()
    assert disparo.skip_reason == DispatchSkipReason.PATIENT_NO_SHOW
    # A inscrição continua viva: faltar não cancela.
    enrollment.refresh_from_db()
    assert enrollment.status in (
        SequenceEnrollmentStatus.ACTIVE,
        SequenceEnrollmentStatus.COMPLETED,
    )


def test_pos_consulta_e_depois_da_ancora_e_nao_deslocamento_positivo(
    clinic_a, trilha_da_consulta, paciente, profissional
):
    """
    RF-SEQ-7.0: um passo de D-0 às 18h numa consulta das 14h é pós-consulta. A
    definição antiga, por deslocamento positivo, deixava esse caso passar e
    mandaria a pesquisa para quem faltou.
    """
    import zoneinfo

    from apps.automation.sequences import inscrever, resolver_disparo

    # Consulta ONTEM às 14h no fuso da clínica. A hora tem de estar na própria
    # consulta: o `post_save` reancora a inscrição na `starts_at` dela, então
    # forçar só a âncora seria desfeito no save seguinte (foi o que este teste
    # me ensinou).
    tz = zoneinfo.ZoneInfo(clinic_a.timezone)
    ontem = (timezone.localtime(timezone.now(), tz) - timedelta(days=1)).replace(
        hour=14, minute=0, second=0, microsecond=0
    )
    consulta = Appointment.objects.create(
        clinic=clinic_a, patient=paciente, practitioner=profissional, starts_at=ontem
    )
    inscrever(
        trilha_da_consulta,
        paciente.patient_contacts.first().contact,
        source=EnrollmentSource.APPOINTMENT,
        patient=paciente,
        appointment=consulta,
        anchor_at=consulta.starts_at,
    )
    consulta.status = AppointmentStatus.NO_SHOW
    consulta.save()

    # Deslocamento ZERO, mas às 18h: cai depois das 14h da consulta.
    enrollment = _passo_pos_consulta_vencido(clinic_a, consulta, offset=0, hora=time(18, 0))
    assert resolver_disparo(enrollment.pk) == "pulado_paciente_faltou"


def test_passo_de_antes_da_consulta_ignora_a_falta(
    clinic_a, trilha_da_consulta, paciente, profissional
):
    """A véspera já saiu no tempo dela: a regra só vale para o que vem depois."""
    from apps.automation.sequences import _e_pos_consulta, horario_do_passo

    consulta = marcar(clinic_a, paciente, profissional)
    enrollment = SequenceEnrollment.objects.get(appointment=consulta)
    vespera = enrollment.sequence.steps.get(order=1)

    previsto = horario_do_passo(vespera, enrollment.anchor_at, clinic_a)
    assert _e_pos_consulta(previsto, enrollment) is False


# ---- o caminho do espelho ----


def test_consulta_espelhada_do_ehr_entra_pela_mesma_porta(
    clinic_a, trilha_da_consulta, paciente, profissional
):
    """
    O pull grava por `update_or_create`, que dispara o MESMO `post_save`. Sem
    isto, consulta marcada na vSaúde nunca entraria em jornada nenhuma.
    """
    consulta, criada = Appointment.all_objects.update_or_create(
        clinic=clinic_a,
        external_id="ehr-123",
        defaults={
            "patient": paciente,
            "practitioner": profissional,
            "starts_at": timezone.now() + timedelta(days=5),
            "status": AppointmentStatus.SCHEDULED,
        },
    )

    assert criada
    assert SequenceEnrollment.objects.filter(appointment=consulta).count() == 1


def test_pull_que_muda_a_data_reancora_pelo_mesmo_caminho(
    clinic_a, trilha_da_consulta, paciente, profissional
):
    nova_data = timezone.now() + timedelta(days=12)
    Appointment.all_objects.update_or_create(
        clinic=clinic_a,
        external_id="ehr-123",
        defaults={
            "patient": paciente,
            "practitioner": profissional,
            "starts_at": timezone.now() + timedelta(days=5),
            "status": AppointmentStatus.SCHEDULED,
        },
    )
    consulta, _ = Appointment.all_objects.update_or_create(
        clinic=clinic_a,
        external_id="ehr-123",
        defaults={
            "patient": paciente,
            "practitioner": profissional,
            "starts_at": nova_data,
            "status": AppointmentStatus.SCHEDULED,
        },
    )

    enrollment = SequenceEnrollment.objects.get(appointment=consulta)
    assert enrollment.anchor_at == nova_data
    assert SequenceEnrollment.objects.count() == 1
