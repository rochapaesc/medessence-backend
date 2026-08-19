"""
As duas saídas configuráveis (RF-SEQ-6.2): quem responde sai e quem marca
consulta sai.

Existem porque a sequência virou a campanha (RF-SEQ-3.7): numa trilha de três
passos, quem respondeu ao primeiro receberia os outros dois assim mesmo. São
por sequência, e não regra global, porque a jornada de atendimento continua
depois de o paciente responder - ela quer que ele responda.
"""

from datetime import time, timedelta

import pytest
from django.utils import timezone

from apps.automation.choices import (
    EnrollmentEndReason,
    EnrollmentSource,
    FlowStatus,
    SequenceDispatchStatus,
    SequenceEnrollmentStatus,
)
from apps.automation.models import Sequence, SequenceDispatch, SequenceStep
from apps.automation.sequences import contato_do_paciente, inscrever, saidas_padrao
from apps.automation.tests.conftest import make_contact, make_flow, make_inbox
from apps.patients.models import Patient, PatientContact
from apps.scheduling.models import Appointment, Practitioner

pytestmark = pytest.mark.django_db

URL = "/api/v1/sequences/"


def trilha(
    clinic,
    *,
    nome="Resgate",
    sai_ao_responder=False,
    sai_ao_agendar=False,
    ancorada=False,
):
    sequence = Sequence.objects.create(
        clinic=clinic,
        name=nome,
        is_active=True,
        exit_on_reply=sai_ao_responder,
        exit_on_appointment=sai_ao_agendar,
        enroll_on_appointment=ancorada,
    )
    SequenceStep.objects.create(
        sequence=sequence,
        order=1,
        offset_days=0,
        send_time=time(8, 0),
        flow=make_flow(clinic, name=f"Fluxo {nome}", status=FlowStatus.ACTIVE),
    )
    return sequence


def ja_recebeu(enrollment):
    """A trilha já falou com essa pessoa: é o que torna a resposta uma resposta."""
    SequenceDispatch.objects.create(
        enrollment=enrollment,
        step=enrollment.sequence.steps.first(),
        scheduled_for=timezone.now(),
        resolved_at=timezone.now(),
        status=SequenceDispatchStatus.STARTED,
    )
    return enrollment


def responder(inbox, texto="quero sim"):
    """O caminho REAL, do webhook da Meta até o sinal da ingestão."""
    from apps.inbox.services import ingest_events
    from apps.integrations.whatsapp.events import parse_meta_webhook
    from apps.integrations.whatsapp.fake.adapter import build_inbound_payload

    payload = build_inbound_payload(wa_id=inbox["contact"].wa_id, body=texto)
    return ingest_events(inbox["channel"], parse_meta_webhook(payload))


# ---- quem responde sai ----


def test_quem_responde_sai_da_trilha_que_pede_isso(clinic_a):
    inbox = make_inbox(clinic_a)
    enrollment = ja_recebeu(
        inscrever(
            trilha(clinic_a, sai_ao_responder=True),
            inbox["contact"],
            source=EnrollmentSource.BATCH,
        )
    )

    responder(inbox)

    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.CANCELED
    assert enrollment.end_reason == EnrollmentEndReason.REPLIED
    # Sem isto a varredura ainda acharia a inscrição na fila do relógio.
    assert enrollment.next_dispatch_at is None


def test_trilha_de_atendimento_segue_depois_da_resposta(clinic_a):
    """A jornada não acaba porque o paciente falou: é o que ela queria."""
    inbox = make_inbox(clinic_a)
    enrollment = ja_recebeu(
        inscrever(
            trilha(clinic_a, sai_ao_responder=False),
            inbox["contact"],
            source=EnrollmentSource.BATCH,
        )
    )

    responder(inbox)

    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.ACTIVE


def test_resposta_antes_do_primeiro_disparo_nao_tira_ninguem(clinic_a):
    """
    Sem disparo não há o que responder. Tirar aqui perderia da campanha
    justamente quem ela ainda não alcançou.
    """
    inbox = make_inbox(clinic_a)
    enrollment = inscrever(
        trilha(clinic_a, sai_ao_responder=True),
        inbox["contact"],
        source=EnrollmentSource.BATCH,
    )

    responder(inbox)

    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.ACTIVE


def test_sai_de_uma_trilha_e_fica_na_outra(clinic_a):
    """A saída é POR SEQUÊNCIA: o mesmo número pode estar em duas."""
    inbox = make_inbox(clinic_a)
    campanha = ja_recebeu(
        inscrever(
            trilha(clinic_a, nome="Campanha", sai_ao_responder=True),
            inbox["contact"],
            source=EnrollmentSource.BATCH,
        )
    )
    jornada = ja_recebeu(
        inscrever(
            trilha(clinic_a, nome="Jornada", sai_ao_responder=False),
            inbox["contact"],
            source=EnrollmentSource.BATCH,
        )
    )

    responder(inbox)

    campanha.refresh_from_db()
    jornada.refresh_from_db()
    assert campanha.status == SequenceEnrollmentStatus.CANCELED
    assert jornada.status == SequenceEnrollmentStatus.ACTIVE


def test_falha_da_saida_nao_derruba_a_ingestao(clinic_a, monkeypatch):
    """
    São dois `try` e não um: a arrumação do calendário não pode engolir a
    mensagem do paciente, que é o que a recepção precisa ver.
    """

    def explode(*args, **kwargs):
        raise RuntimeError("saída quebrada")

    monkeypatch.setattr("apps.automation.sequences.aplicar_saida_por_resposta", explode)
    inbox = make_inbox(clinic_a)

    stats = responder(inbox)

    assert stats["inbound"] == 1


# ---- quem marca consulta sai ----


@pytest.fixture
def paciente(clinic_a):
    patient = Patient.objects.create(clinic=clinic_a, name="Ivanita")
    PatientContact.objects.create(patient=patient, contact=make_contact(clinic_a))
    return patient


@pytest.fixture
def profissional(clinic_a):
    return Practitioner.objects.create(clinic=clinic_a, name="Dra. Alfa")


def marcar(clinic, paciente, profissional, *, daqui=timedelta(days=10)):
    return Appointment.objects.create(
        clinic=clinic,
        patient=paciente,
        practitioner=profissional,
        starts_at=timezone.now() + daqui,
    )


def na_campanha(clinic, paciente, *, nome="Volte a nos visitar"):
    sequence = trilha(clinic, nome=nome, sai_ao_agendar=True)
    return inscrever(
        sequence,
        contato_do_paciente(paciente),
        source=EnrollmentSource.BATCH,
        patient=paciente,
    )


def test_marcar_consulta_tira_da_campanha(clinic_a, paciente, profissional):
    enrollment = na_campanha(clinic_a, paciente)

    marcar(clinic_a, paciente, profissional)

    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.CANCELED
    assert enrollment.end_reason == EnrollmentEndReason.SCHEDULED


def test_trilha_ancorada_na_consulta_nao_perde_quem_acabou_de_entrar(
    clinic_a, paciente, profissional
):
    """
    Onde a consulta é a porta de ENTRADA, quem marca entra. Tirar ali seria a
    jornada expulsar exatamente quem ela acabou de receber.
    """
    sequence = trilha(
        clinic_a, nome="Jornada", sai_ao_agendar=True, ancorada=True
    )
    enrollment = inscrever(
        sequence,
        contato_do_paciente(paciente),
        source=EnrollmentSource.BATCH,
        patient=paciente,
    )

    marcar(clinic_a, paciente, profissional)

    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.ACTIVE


def test_campanha_com_a_chave_desligada_segue(clinic_a, paciente, profissional):
    sequence = trilha(clinic_a, nome="Sem saída", sai_ao_agendar=False)
    enrollment = inscrever(
        sequence,
        contato_do_paciente(paciente),
        source=EnrollmentSource.BATCH,
        patient=paciente,
    )

    marcar(clinic_a, paciente, profissional)

    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.ACTIVE


def test_consulta_passada_do_backfill_nao_tira_ninguem(clinic_a, paciente, profissional):
    """
    O espelho da vSaúde traz 10.508 consultas, quase todas no passado. Consulta
    velha não é alguém marcando consulta agora.
    """
    enrollment = na_campanha(clinic_a, paciente)

    marcar(clinic_a, paciente, profissional, daqui=timedelta(days=-30))

    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.ACTIVE


def test_regravar_a_mesma_consulta_nao_refaz_a_conta(clinic_a, paciente, profissional):
    """
    O pull da agenda regrava as futuras a cada 5 minutos por `update_or_create`.
    Sem o `created`, cada passada refaria a busca para quem já saiu.
    """
    enrollment = na_campanha(clinic_a, paciente)
    consulta = marcar(clinic_a, paciente, profissional)
    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.CANCELED

    outra = na_campanha(clinic_a, paciente, nome="Segunda campanha")
    consulta.save()

    outra.refresh_from_db()
    assert outra.status == SequenceEnrollmentStatus.ACTIVE


# ---- como as duas nascem ----


def test_divulgacao_nasce_com_as_duas_ligadas():
    assert saidas_padrao(is_marketing=True, enroll_on_appointment=False) == {
        "exit_on_reply": True,
        "exit_on_appointment": True,
    }


def test_atendimento_nasce_com_as_duas_desligadas():
    assert saidas_padrao(is_marketing=False, enroll_on_appointment=False) == {
        "exit_on_reply": False,
        "exit_on_appointment": False,
    }


def test_ancorada_na_consulta_nao_nasce_com_a_saida_por_consulta():
    """Guardar ligado o que o motor ignora obrigaria a tela a explicar uma chave morta."""
    assert saidas_padrao(is_marketing=True, enroll_on_appointment=True) == {
        "exit_on_reply": True,
        "exit_on_appointment": False,
    }


def test_criar_pela_api_aplica_o_padrao_do_tipo(api_client, manager_single_clinic, clinic_a):
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.post(
        URL, {"name": "Campanha de agosto", "is_marketing": True}, format="json"
    )

    assert response.status_code == 201
    assert response.data["exit_on_reply"] is True
    assert response.data["exit_on_appointment"] is True


def test_valor_explicito_vence_o_padrao(api_client, manager_single_clinic, clinic_a):
    """É padrão de NASCIMENTO, não regra de negócio: quem disser vence."""
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.post(
        URL,
        {"name": "Campanha teimosa", "is_marketing": True, "exit_on_reply": False},
        format="json",
    )

    assert response.status_code == 201
    assert response.data["exit_on_reply"] is False
