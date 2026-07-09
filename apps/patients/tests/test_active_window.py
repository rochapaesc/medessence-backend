"""
Janela de atividade configurável (RF-PAC-2): padrão da clínica
(Clinic.active_window_days) com override por profissional na visão da
carteira (Practitioner.active_window_days).
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.patients.models import Patient
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import Appointment, Practitioner

URL = "/api/v1/patients/"


@pytest.fixture
def patient_40_days(clinic_a):
    return Patient.objects.create(
        clinic=clinic_a,
        name="Paciente Quarenta Dias",
        last_appointment_at=timezone.now() - timedelta(days=40),
    )


def test_janela_da_clinica_e_configuravel(
    api_client, manager_single_clinic, clinic_a, patient_40_days
):
    api_client.force_authenticate(manager_single_clinic)

    # Janela padrão (90d): consulta há 40 dias → ATIVO
    response = api_client.get(URL, {"status": "active"})
    assert [i["name"] for i in response.data["results"]] == ["Paciente Quarenta Dias"]
    assert response.data["results"][0]["status"] == "active"

    # Clínica com janela de 30 dias → o mesmo paciente vira INATIVO
    clinic_a.active_window_days = 30
    clinic_a.save(update_fields=["active_window_days"])

    response = api_client.get(URL, {"status": "active"})
    assert response.data["results"] == []
    inactive = api_client.get(URL, {"status": "inactive"})
    assert [i["name"] for i in inactive.data["results"]] == ["Paciente Quarenta Dias"]
    assert inactive.data["results"][0]["status"] == "inactive"

    counters = api_client.get(f"{URL}counters/")
    assert counters.data == {"total": 1, "active": 0, "inactive": 1}


def test_override_do_profissional_na_carteira(
    api_client, manager_single_clinic, clinic_a, patient_40_days
):
    """Clínica 30d; profissional com override de 180d → paciente ativo NA CARTEIRA dele."""
    clinic_a.active_window_days = 30
    clinic_a.save(update_fields=["active_window_days"])

    practitioner = Practitioner.objects.create(
        clinic=clinic_a, name="Dra. Derma", active_window_days=180
    )
    Appointment.objects.create(
        clinic=clinic_a,
        patient=patient_40_days,
        practitioner=practitioner,
        starts_at=timezone.now() - timedelta(days=40),
        status=AppointmentStatus.COMPLETED,
    )
    api_client.force_authenticate(manager_single_clinic)

    # Visão da clínica (30d): inativo
    assert api_client.get(f"{URL}counters/").data == {"total": 1, "active": 0, "inactive": 1}

    # Carteira da profissional (180d): ativo
    carteira = api_client.get(f"{URL}counters/", {"practitioner": practitioner.pk})
    assert carteira.data == {"total": 1, "active": 1, "inactive": 0}

    listagem = api_client.get(URL, {"status": "active", "practitioner": practitioner.pk})
    assert [i["name"] for i in listagem.data["results"]] == ["Paciente Quarenta Dias"]


def test_atividade_da_carteira_e_relativa_ao_profissional(
    api_client, manager_single_clinic, clinic_a
):
    """Consulta recente com OUTRO profissional não torna o paciente ativo na carteira."""
    dra = Practitioner.objects.create(clinic=clinic_a, name="Dra. A")
    dr_outro = Practitioner.objects.create(clinic=clinic_a, name="Dr. Outro")
    patient = Patient.objects.create(clinic=clinic_a, name="Paciente Dividido")

    # Consulta antiga com a Dra. A (200 dias) e recente com o Dr. Outro (10 dias)
    for practitioner, days in [(dra, 200), (dr_outro, 10)]:
        Appointment.objects.create(
            clinic=clinic_a,
            patient=patient,
            practitioner=practitioner,
            starts_at=timezone.now() - timedelta(days=days),
            status=AppointmentStatus.COMPLETED,
        )
    api_client.force_authenticate(manager_single_clinic)

    # Na clínica (90d): ativo (consulta de 10 dias atrás)
    assert api_client.get(f"{URL}counters/").data["active"] == 1

    # Na carteira da Dra. A (90d, sem override): inativo — a consulta COM ELA é antiga
    carteira = api_client.get(f"{URL}counters/", {"practitioner": dra.pk})
    assert carteira.data == {"total": 1, "active": 0, "inactive": 1}


def test_counters_com_profissional_inexistente_retorna_400(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(f"{URL}counters/", {"practitioner": 9999})
    assert response.status_code == 400
