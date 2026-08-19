"""
API das sequências (RF-SEQ-3.2 e RF-SEQ-10).

Duas coisas: a porta da ficha, que precisa EXPLICAR a recusa em vez de calar,
e os papéis - montar é do gestor, inscrever é de quem atende.
"""

from datetime import time

import pytest

from apps.automation.choices import EnrollmentSource, FlowStatus, SequenceEnrollmentStatus
from apps.automation.models import Sequence, SequenceEnrollment, SequenceStep
from apps.automation.sequences import inscrever
from apps.automation.tests.conftest import make_contact, make_flow
from apps.patients.models import Patient, PatientContact

pytestmark = pytest.mark.django_db

URL = "/api/v1/sequences/"


@pytest.fixture
def trilha(clinic_a):
    sequence = Sequence.objects.create(clinic=clinic_a, name="Pós-consulta", is_active=True)
    SequenceStep.objects.create(
        sequence=sequence,
        order=1,
        offset_days=1,
        send_time=time(8, 0),
        flow=make_flow(clinic_a, name="Aviso", status=FlowStatus.ACTIVE),
    )
    return sequence


@pytest.fixture
def paciente_com_numero(clinic_a):
    patient = Patient.objects.create(clinic=clinic_a, name="Ivanita")
    PatientContact.objects.create(patient=patient, contact=make_contact(clinic_a))
    return patient


# ---- a porta da ficha ----


def test_gestor_inscreve_pela_ficha(
    api_client, manager_single_clinic, clinic_a, trilha, paciente_com_numero
):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        f"{URL}{trilha.pk}/enroll/", {"patient": paciente_com_numero.pk}, format="json"
    )

    assert response.status_code == 201
    assert response.data["source"] == EnrollmentSource.PATIENT_RECORD
    assert SequenceEnrollment.objects.filter(sequence=trilha).count() == 1


def test_atendente_tambem_inscreve(
    api_client, attendant_a, clinic_a, trilha, paciente_com_numero
):
    """Montar é do gestor; colocar alguém na trilha é de quem atende (RF-SEQ-10)."""
    api_client.force_authenticate(attendant_a)
    response = api_client.post(
        f"{URL}{trilha.pk}/enroll/", {"patient": paciente_com_numero.pk}, format="json"
    )
    assert response.status_code == 201


def test_atendente_nao_cria_sequencia(api_client, attendant_a, clinic_a):
    api_client.force_authenticate(attendant_a)
    response = api_client.post(URL, {"name": "Minha trilha"}, format="json")
    assert response.status_code == 403


def test_paciente_sem_numero_recebe_o_motivo_escrito(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """Recusa explicada: quem está na tela precisa saber por que não entrou."""
    sem_numero = Patient.objects.create(clinic=clinic_a, name="Sem WhatsApp")
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        f"{URL}{trilha.pk}/enroll/", {"patient": sem_numero.pk}, format="json"
    )

    assert response.status_code == 400
    assert "número" in str(response.data).lower()


def test_inscrever_duas_vezes_explica_em_vez_de_calar(
    api_client, manager_single_clinic, clinic_a, trilha, paciente_com_numero
):
    api_client.force_authenticate(manager_single_clinic)
    payload = {"patient": paciente_com_numero.pk}
    assert api_client.post(f"{URL}{trilha.pk}/enroll/", payload, format="json").status_code == 201

    segunda = api_client.post(f"{URL}{trilha.pk}/enroll/", payload, format="json")
    assert segunda.status_code == 400
    assert "já está" in str(segunda.data).lower()


def test_contato_com_opt_out_recebe_o_motivo(
    api_client, manager_single_clinic, clinic_a, trilha, paciente_com_numero
):
    contact = paciente_com_numero.patient_contacts.first().contact
    contact.marketing_opt_out = True
    contact.save(update_fields=["marketing_opt_out"])

    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        f"{URL}{trilha.pk}/enroll/", {"patient": paciente_com_numero.pk}, format="json"
    )
    assert response.status_code == 400
    assert "marketing" in str(response.data).lower()


def test_remover_pela_ficha_e_idempotente(
    api_client, manager_single_clinic, clinic_a, trilha, paciente_com_numero
):
    api_client.force_authenticate(manager_single_clinic)
    api_client.post(f"{URL}{trilha.pk}/enroll/", {"patient": paciente_com_numero.pk}, format="json")

    payload = {"patient": paciente_com_numero.pk}
    assert api_client.post(f"{URL}{trilha.pk}/unenroll/", payload, format="json").status_code == 200
    # Sair de onde não se está também é 200.
    assert api_client.post(f"{URL}{trilha.pk}/unenroll/", payload, format="json").status_code == 200

    enrollment = SequenceEnrollment.objects.get(sequence=trilha)
    assert enrollment.status == SequenceEnrollmentStatus.CANCELED


# ---- escopo e listagem ----


def test_lista_nao_vaza_sequencia_de_outra_clinica(
    api_client, manager_single_clinic, clinic_a, clinic_b, trilha
):
    Sequence.objects.create(clinic=clinic_b, name="Trilha da outra")
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.get(URL)
    assert response.status_code == 200
    nomes = [item["name"] for item in response.data["results"]]
    assert nomes == ["Pós-consulta"]


def test_aposentar_a_sequencia_cancela_quem_esta_dentro(
    api_client, manager_single_clinic, clinic_a, trilha, paciente_com_numero
):
    contact = paciente_com_numero.patient_contacts.first().contact
    enrollment = inscrever(trilha, contact, source=EnrollmentSource.BATCH)

    api_client.force_authenticate(manager_single_clinic)
    assert api_client.delete(f"{URL}{trilha.pk}/").status_code == 204

    enrollment.refresh_from_db()
    assert enrollment.status == SequenceEnrollmentStatus.CANCELED


def test_passo_novo_entra_no_lugar_do_relogio_dele(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """
    RF-SEQ-2.2: a posição é consequência do prazo. Um passo criado depois, mas
    com prazo ANTERIOR, entra na frente sozinho, e quem chamou não escolhe
    `order` (o campo é só de leitura).
    """
    flow = trilha.steps.first().flow
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.post(
        "/api/v1/sequence-steps/",
        {
            "sequence": trilha.pk,
            "name": "Véspera",
            "offset_days": -1,
            "send_time": "08:00",
            "flow": flow.pk,
            "order": 99,  # ignorado de propósito
        },
        format="json",
    )
    assert resposta.status_code == 201

    ordens = {p.name: p.order for p in trilha.steps.all()}
    assert ordens["Véspera"] == 1
    # O que já existia (D+1) foi empurrado para trás pelo relógio.
    assert sorted(ordens.values()) == [1, 2]


def test_passo_avisa_quando_o_fluxo_esta_em_rascunho(
    api_client, manager_single_clinic, clinic_a, trilha
):
    """A tela precisa avisar ANTES: passo com fluxo em rascunho não dispara."""
    from apps.automation.models import Flow

    rascunho = Flow.objects.create(clinic=clinic_a, name="Ainda não")
    SequenceStep.objects.create(
        sequence=trilha, order=2, offset_days=5, send_time=time(9, 0), flow=rascunho
    )

    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(f"{URL}{trilha.pk}/")

    passos = {p["order"]: p["flow_published"] for p in response.data["steps"]}
    assert passos == {1: True, 2: False}


# ---- apagar a trilha e os fluxos dos passos (RF-SEQ-12.2) ----


def _trilha_de_modelo(clinic, nome="Vinda de modelo"):
    """Como um modelo cria: um fluxo em RASCUNHO por passo."""
    from apps.automation.models import Flow

    sequence = Sequence.objects.create(clinic=clinic, name=nome)
    fluxos = []
    for ordem in (1, 2):
        flow = Flow.objects.create(clinic=clinic, name=f"{nome}: passo {ordem}")
        SequenceStep.objects.create(
            sequence=sequence,
            order=ordem,
            offset_days=ordem,
            send_time=time(9, 0),
            flow=flow,
        )
        fluxos.append(flow)
    return sequence, fluxos


def test_apagar_leva_os_fluxos_dos_passos_quando_pedido(
    api_client, manager_single_clinic, clinic_a
):
    """
    Duas trilhas apagadas deixaram OITO rascunhos órfãos na lista de Fluxos da
    clínica real, sem nada dizendo de onde vinham.
    """
    from apps.automation.models import Flow

    sequence, fluxos = _trilha_de_modelo(clinic_a)
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.delete(f"{URL}{sequence.pk}/?apagar_fluxos=1")

    assert resposta.status_code == 204
    assert not Flow.objects.filter(pk__in=[f.pk for f in fluxos]).exists()


def test_sem_pedir_os_fluxos_ficam(api_client, manager_single_clinic, clinic_a):
    from apps.automation.models import Flow

    sequence, fluxos = _trilha_de_modelo(clinic_a)
    api_client.force_authenticate(manager_single_clinic)

    api_client.delete(f"{URL}{sequence.pk}/")

    assert Flow.objects.filter(pk__in=[f.pk for f in fluxos]).count() == 2


def test_fluxo_publicado_nao_e_apagado(
    api_client, manager_single_clinic, clinic_a
):
    """Publicado pode estar atendendo alguém agora."""
    from apps.automation.choices import FlowStatus
    from apps.automation.models import Flow

    sequence, fluxos = _trilha_de_modelo(clinic_a)
    publicado = fluxos[0]
    publicado.status = FlowStatus.ACTIVE
    publicado.save(update_fields=["status"])

    api_client.force_authenticate(manager_single_clinic)
    api_client.delete(f"{URL}{sequence.pk}/?apagar_fluxos=1")

    assert Flow.objects.filter(pk=publicado.pk).exists()
    assert not Flow.objects.filter(pk=fluxos[1].pk).exists()


def test_fluxo_de_outra_trilha_tambem_nao_e_apagado(
    api_client, manager_single_clinic, clinic_a
):
    """Apagá-lo derrubaria uma sequência que ninguém mandou apagar."""
    from apps.automation.models import Flow

    sequence, fluxos = _trilha_de_modelo(clinic_a)
    vizinha = Sequence.objects.create(clinic=clinic_a, name="A vizinha")
    SequenceStep.objects.create(
        sequence=vizinha, order=1, offset_days=1, send_time=time(9, 0), flow=fluxos[0]
    )

    api_client.force_authenticate(manager_single_clinic)
    api_client.delete(f"{URL}{sequence.pk}/?apagar_fluxos=1")

    assert Flow.objects.filter(pk=fluxos[0].pk).exists()
    assert not Flow.objects.filter(pk=fluxos[1].pk).exists()
