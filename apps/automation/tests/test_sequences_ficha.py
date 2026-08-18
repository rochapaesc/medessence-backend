"""
As sequências vistas pela FICHA do paciente (RF-SEQ-3.2).

O cartão da ficha responde três coisas numa chamada só, e o que estes testes
protegem é a honestidade de cada uma: o passo dito como posição, o que ainda
falta sair, e o contexto do número que decide se dá para inscrever.
"""

from datetime import time

import pytest
from django.utils import timezone

from apps.automation.choices import (
    EnrollmentEndReason,
    EnrollmentSource,
    FlowStatus,
    SequenceEnrollmentStatus,
)
from apps.automation.models import Sequence, SequenceStep
from apps.automation.sequences import inscrever, remover
from apps.automation.tests.conftest import make_contact, make_flow
from apps.patients.models import Patient, PatientContact

pytestmark = pytest.mark.django_db

URL = "/api/v1/sequences/of-patient/"


@pytest.fixture
def trilha(clinic_a):
    sequence = Sequence.objects.create(
        clinic=clinic_a, name="Pós-consulta", is_active=True, is_marketing=False
    )
    for i, nome in enumerate(["Véspera", "Protocolo", "Avaliação"], start=1):
        SequenceStep.objects.create(
            sequence=sequence,
            order=i,
            name=nome,
            offset_days=i - 1,
            send_time=time(9, 0),
            flow=make_flow(clinic_a, name=f"Fluxo {i}", status=FlowStatus.ACTIVE),
        )
    return sequence


def paciente_com_numero(clinic, nome, wa_id):
    patient = Patient.objects.create(clinic=clinic, name=nome)
    contact = make_contact(clinic, wa_id=wa_id)
    PatientContact.objects.create(patient=patient, contact=contact, is_primary=True)
    return patient, contact


def test_diz_o_passo_como_posicao_e_quantas_faltam(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    ⚠️ `current_step` é o passo que AINDA VAI sair. No primeiro passo de três,
    faltam TRÊS mensagens, não duas: é sobre esse número que a recepção decide
    tirar alguém da trilha.
    """
    api_client.force_authenticate(manager_single_clinic)
    patient, contact = paciente_com_numero(clinic_a, "Ana", "5585900000901")
    inscrever(trilha, contact, source=EnrollmentSource.PATIENT_RECORD, patient=patient)

    response = api_client.get(URL, {"patient": patient.pk})

    assert response.status_code == 200
    ativa = response.data["ativas"][0]
    assert ativa["passo_atual"] == "Véspera"
    assert ativa["passo_numero"] == 1
    assert ativa["passos_total"] == 3
    assert ativa["faltam"] == 3
    assert ativa["sequence_name"] == "Pós-consulta"
    assert ativa["marketing"] is False
    assert ativa["sequencia_ligada"] is True


def test_segue_o_CONTATO_e_nao_so_o_paciente(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    Inscrição nascida de nó de fluxo tem só o contato. Filtrar por paciente
    faria a ficha dizer "não está em nenhuma" com o número dentro da trilha.
    """
    api_client.force_authenticate(manager_single_clinic)
    patient, contact = paciente_com_numero(clinic_a, "Bruno", "5585900000902")
    inscrever(trilha, contact, source=EnrollmentSource.FLOW_NODE)

    response = api_client.get(URL, {"patient": patient.pk})

    assert response.status_code == 200
    assert len(response.data["ativas"]) == 1


def test_avisa_quando_o_numero_e_de_mais_de_um_paciente(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """Tirar da trilha tira o número, e com ele a outra pessoa."""
    api_client.force_authenticate(manager_single_clinic)
    mae, contact = paciente_com_numero(clinic_a, "Mãe", "5585900000903")
    filho = Patient.objects.create(clinic=clinic_a, name="Filho")
    PatientContact.objects.create(patient=filho, contact=contact)

    response = api_client.get(URL, {"patient": mae.pk})

    assert response.data["contato"]["pacientes_no_numero"] == 2


def test_sem_numero_responde_sem_contato_em_vez_de_estourar(
    api_client, manager_single_clinic, clinic_a, trilha
):
    api_client.force_authenticate(manager_single_clinic)
    patient = Patient.objects.create(clinic=clinic_a, name="Sem WhatsApp")

    response = api_client.get(URL, {"patient": patient.pk})

    assert response.status_code == 200
    assert response.data["contato"]["id"] is None
    assert response.data["contato"]["pacientes_no_numero"] == 0
    assert response.data["ativas"] == []


def test_opt_out_aparece_para_a_tela_poder_explicar(
    api_client, manager_single_clinic, clinic_a, trilha
):
    api_client.force_authenticate(manager_single_clinic)
    patient, contact = paciente_com_numero(clinic_a, "Calado", "5585900000904")
    contact.marketing_opt_out = True
    contact.save(update_fields=["marketing_opt_out"])

    response = api_client.get(URL, {"patient": patient.pk})

    assert response.data["contato"]["opt_out"] is True


def test_quem_saiu_vai_para_o_historico_com_o_motivo(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    ⚠️ Os motivos são os SEIS do enum, e "marcou consulta" não é um deles:
    marcar consulta pula um passo pós-consulta, não encerra a inscrição. Quem
    responde se a trilha deu certo é a medição do painel, por disparo.
    """
    api_client.force_authenticate(manager_single_clinic)
    patient, contact = paciente_com_numero(clinic_a, "Voltou", "5585900000905")
    enrollment = inscrever(
        trilha, contact, source=EnrollmentSource.PATIENT_RECORD, patient=patient
    )
    remover(enrollment, reason=EnrollmentEndReason.MANUAL)

    response = api_client.get(URL, {"patient": patient.pk})

    assert response.data["ativas"] == []
    assert len(response.data["historico"]) == 1
    assert response.data["historico"][0]["end_reason"] == EnrollmentEndReason.MANUAL
    assert response.data["historico"][0]["sequence_name"] == "Pós-consulta"


def test_segurado_diz_desde_quando_e_por_que(
    api_client, manager_single_clinic, clinic_a, trilha
):
    api_client.force_authenticate(manager_single_clinic)
    patient, contact = paciente_com_numero(clinic_a, "Parada", "5585900000906")
    enrollment = inscrever(
        trilha, contact, source=EnrollmentSource.PATIENT_RECORD, patient=patient
    )
    enrollment.held_since = timezone.now()
    enrollment.hold_reason = "no_window"
    enrollment.save(update_fields=["held_since", "hold_reason"])

    response = api_client.get(URL, {"patient": patient.pk})

    ativa = response.data["ativas"][0]
    assert ativa["held_since"] is not None
    assert ativa["hold_reason"] == "no_window"


def test_paciente_de_outra_clinica_nao_vaza(
    api_client, manager_single_clinic, clinic_b, trilha
):
    api_client.force_authenticate(manager_single_clinic)
    de_fora = Patient.objects.create(clinic=clinic_b, name="De outra clínica")

    response = api_client.get(URL, {"patient": de_fora.pk})

    assert response.status_code == 400


def test_recepcao_enxerga_a_ficha(api_client, attendant_a, clinic_a, trilha):
    """É a porta da recepção (RF-SEQ-10.1): sem ler, ela não tem o que inscrever."""
    api_client.force_authenticate(attendant_a)
    patient, _ = paciente_com_numero(clinic_a, "Ana", "5585900000907")

    response = api_client.get(URL, {"patient": patient.pk})

    assert response.status_code == 200


def test_passo_no_meio_conta_o_que_ainda_falta(
    api_client, manager_single_clinic, clinic_a, trilha
):
    api_client.force_authenticate(manager_single_clinic)
    patient, contact = paciente_com_numero(clinic_a, "No meio", "5585900000908")
    enrollment = inscrever(
        trilha, contact, source=EnrollmentSource.PATIENT_RECORD, patient=patient
    )
    ultimo = trilha.steps.order_by("offset_days").last()
    enrollment.current_step = ultimo
    enrollment.save(update_fields=["current_step"])

    response = api_client.get(URL, {"patient": patient.pk})

    ativa = response.data["ativas"][0]
    assert ativa["passo_numero"] == 3
    assert ativa["faltam"] == 1
