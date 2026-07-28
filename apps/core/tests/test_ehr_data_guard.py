"""
Trava de dado de produção (`EHR_DATA_GUARD`) — TEMPORÁRIA.

Existe porque o desenvolvimento roda sobre a clínica REAL: um DELETE aqui não
para aqui, o write-through o traduz para o `Delete` do EHR e apaga do
prontuário de verdade.

O que a trava NÃO pode fazer: atrapalhar o trabalho com dado local. Metade
destes testes cuida disso.
"""

import pytest

from apps.patients.models import Patient
from apps.scheduling.models import Appointment, Practitioner
from apps.tenants.choices import EHRProviderKind

PATIENTS_URL = "/api/v1/patients/"
APPOINTMENTS_URL = "/api/v1/appointments/"


@pytest.fixture
def ehr_clinic(clinic_a):
    clinic_a.ehr_provider = EHRProviderKind.VSAUDE
    clinic_a.save(update_fields=["ehr_provider"])
    return clinic_a


@pytest.fixture
def logado(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


def test_paciente_do_ehr_nao_pode_ser_excluido(logado, ehr_clinic):
    paciente = Patient.objects.create(
        clinic=ehr_clinic, name="Paciente do prontuário", external_id="vsaude-123"
    )

    response = logado.delete(f"{PATIENTS_URL}{paciente.pk}/")

    assert response.status_code == 403
    assert "prontuário" in str(response.data["detail"])
    paciente.refresh_from_db()
    assert paciente.deleted_at is None, "nem soft delete pode acontecer"


def test_consulta_do_ehr_nao_pode_ser_excluida(logado, ehr_clinic):
    paciente = Patient.objects.create(clinic=ehr_clinic, name="X", external_id="v-1")
    practitioner = Practitioner.objects.create(clinic=ehr_clinic, name="Dr. Y")
    from django.utils import timezone

    consulta = Appointment.objects.create(
        clinic=ehr_clinic,
        patient=paciente,
        practitioner=practitioner,
        starts_at=timezone.now(),
        duration_min=30,
        external_id="vsaude-ag-1",
    )

    response = logado.delete(f"{APPOINTMENTS_URL}{consulta.pk}/")

    assert response.status_code == 403
    consulta.refresh_from_db()
    assert consulta.deleted_at is None


def test_a_trava_nao_enfileira_push(logado, ehr_clinic):
    """
    O ponto da trava: barrar ANTES de o write-through virar `Delete` no EHR.
    Se a operação fosse enfileirada, o estrago já estaria a caminho.
    """
    from apps.integrations.models import SyncOperation

    paciente = Patient.objects.create(
        clinic=ehr_clinic, name="Paciente do prontuário", external_id="vsaude-999"
    )

    logado.delete(f"{PATIENTS_URL}{paciente.pk}/")

    assert not SyncOperation.objects.filter(clinic=ehr_clinic).exists()


# ------------------- o que a trava NÃO pode atrapalhar ------------------- #


def test_paciente_LOCAL_continua_apagavel(logado, ehr_clinic):
    """Cadastro nosso é dado nosso: a trava só protege o que veio do EHR."""
    paciente = Patient.objects.create(clinic=ehr_clinic, name="Cadastrado aqui")

    response = logado.delete(f"{PATIENTS_URL}{paciente.pk}/")

    assert response.status_code == 204
    paciente.refresh_from_db()
    assert paciente.deleted_at is not None


def test_clinica_sem_ehr_nao_e_afetada(logado, clinic_a):
    """Clínica sem prontuário externo não tem o que propagar."""
    assert not clinic_a.ehr_provider
    paciente = Patient.objects.create(
        clinic=clinic_a, name="De clínica sem EHR", external_id="qualquer"
    )

    response = logado.delete(f"{PATIENTS_URL}{paciente.pk}/")

    assert response.status_code == 204


def test_edicao_e_criacao_seguem_liberadas(logado, ehr_clinic):
    """A trava é só sobre EXCLUIR — o trabalho do dia a dia continua."""
    paciente = Patient.objects.create(
        clinic=ehr_clinic, name="Nome antigo", external_id="vsaude-77"
    )

    response = logado.patch(
        f"{PATIENTS_URL}{paciente.pk}/", {"name": "Nome novo"}, format="json"
    )

    assert response.status_code == 200
    paciente.refresh_from_db()
    assert paciente.name == "Nome novo"


def test_desligada_por_setting_libera(logado, ehr_clinic, settings):
    """Precisa ser reversível por ambiente — é temporária, não permanente."""
    settings.EHR_DATA_GUARD = False
    paciente = Patient.objects.create(
        clinic=ehr_clinic, name="Do prontuário", external_id="vsaude-555"
    )

    response = logado.delete(f"{PATIENTS_URL}{paciente.pk}/")

    assert response.status_code == 204


def test_front_sabe_que_a_trava_esta_ligada(logado, ehr_clinic):
    """Sem isto o botão de excluir promete o que a API recusa."""
    response = logado.get("/api/v1/me/memberships/")

    clinica = response.data[0]["clinic"]
    assert clinica["data_guard"] is True
