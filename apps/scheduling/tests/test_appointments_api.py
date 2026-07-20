"""API da agenda (RF-AGE-1/2) - escopo, validação de FKs e denormalização."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.patients.models import Patient
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import Appointment, Practitioner

URL = "/api/v1/appointments/"


@pytest.fixture
def crm_a(clinic_a):
    return {
        "patient": Patient.objects.create(clinic=clinic_a, name="Paciente A"),
        "practitioner": Practitioner.objects.create(clinic=clinic_a, name="Dra. Alfa"),
    }


@pytest.fixture
def crm_b(clinic_b):
    return {
        "patient": Patient.objects.create(clinic=clinic_b, name="Paciente B"),
        "practitioner": Practitioner.objects.create(clinic=clinic_b, name="Dr. Beta"),
    }


def _payload(crm, **overrides):
    data = {
        "patient": crm["patient"].pk,
        "practitioner": crm["practitioner"].pk,
        "starts_at": (timezone.now() - timedelta(days=1)).isoformat(),
        "status": AppointmentStatus.COMPLETED,
    }
    data.update(overrides)
    return data


def test_create_recalcula_last_appointment(api_client, manager_single_clinic, crm_a):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(URL, _payload(crm_a), format="json")
    assert response.status_code == 201

    patient = crm_a["patient"]
    patient.refresh_from_db()
    assert patient.last_appointment_at is not None
    assert patient.status == "active"


def test_consulta_cancelada_nao_conta_para_o_status(api_client, manager_single_clinic, crm_a):
    api_client.force_authenticate(manager_single_clinic)
    api_client.post(URL, _payload(crm_a, status=AppointmentStatus.CANCELED), format="json")
    patient = crm_a["patient"]
    patient.refresh_from_db()
    assert patient.last_appointment_at is None


def test_consulta_futura_nao_torna_paciente_ativo(api_client, manager_single_clinic, crm_a):
    api_client.force_authenticate(manager_single_clinic)
    api_client.post(
        URL,
        _payload(
            crm_a,
            starts_at=(timezone.now() + timedelta(days=5)).isoformat(),
            status=AppointmentStatus.SCHEDULED,
        ),
        format="json",
    )
    patient = crm_a["patient"]
    patient.refresh_from_db()
    assert patient.last_appointment_at is None


def test_paciente_de_outra_clinica_e_rejeitado(
    api_client, manager_two_clinics, clinic_a, crm_a, crm_b
):
    api_client.force_authenticate(manager_two_clinics)
    payload = _payload(crm_a, patient=crm_b["patient"].pk)  # paciente da clínica B
    response = api_client.post(URL, payload, format="json", HTTP_X_CLINIC_ID=str(clinic_a.pk))
    assert response.status_code == 400


def test_filtro_por_intervalo_de_datas(api_client, manager_single_clinic, clinic_a, crm_a):
    now = timezone.now()
    for delta in (-10, -2, 5):
        Appointment.objects.create(
            clinic=clinic_a,
            patient=crm_a["patient"],
            practitioner=crm_a["practitioner"],
            starts_at=now + timedelta(days=delta),
        )
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(
        URL,
        {
            "starts_at_after": (now - timedelta(days=3)).isoformat(),
            "starts_at_before": (now + timedelta(days=30)).isoformat(),
        },
    )
    assert response.status_code == 200
    assert response.data["count"] == 2


def test_lista_escopada(api_client, manager_single_clinic, clinic_a, clinic_b, crm_a, crm_b):
    Appointment.objects.create(
        clinic=clinic_a,
        patient=crm_a["patient"],
        practitioner=crm_a["practitioner"],
        starts_at=timezone.now(),
    )
    Appointment.objects.create(
        clinic=clinic_b,
        patient=crm_b["patient"],
        practitioner=crm_b["practitioner"],
        starts_at=timezone.now(),
    )
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL)
    assert response.data["count"] == 1
    assert response.data["results"][0]["patient_name"] == "Paciente A"
