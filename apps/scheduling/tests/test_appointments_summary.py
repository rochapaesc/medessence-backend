"""
`/appointments/summary/` — contagem agregada da agenda (RF-AGE-1).

Existe para o calendário e os KPIs não baixarem o mês inteiro só para contar
(um mês real passa de 400 consultas, paginadas de 100 em 100).
"""

from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest
from django.utils import timezone

from apps.patients.models import Patient
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import Appointment, Practitioner

URL = "/api/v1/appointments/summary/"
FORTALEZA = ZoneInfo("America/Fortaleza")


@pytest.fixture
def agenda(clinic_a):
    """Julho/2026: 3 consultas no dia 1, 1 no dia 2 e 1 cancelada no dia 3."""
    patient = Patient.objects.create(clinic=clinic_a, name="Paciente Alfa")
    doctor = Practitioner.objects.create(clinic=clinic_a, name="Dra. Alfa")
    other = Practitioner.objects.create(clinic=clinic_a, name="Dr. Beta")

    def _appointment(day, hour, practitioner, status=AppointmentStatus.SCHEDULED):
        return Appointment.objects.create(
            clinic=clinic_a,
            patient=patient,
            practitioner=practitioner,
            starts_at=datetime(2026, 7, day, hour, tzinfo=FORTALEZA),
            status=status,
        )

    return {
        "doctor": doctor,
        "other": other,
        "appointments": [
            _appointment(1, 8, doctor),
            _appointment(1, 9, doctor),
            _appointment(1, 10, other),
            _appointment(2, 8, doctor),
            _appointment(3, 8, doctor, AppointmentStatus.CANCELED),
        ],
    }


def _get(api_client, user, **params):
    api_client.force_authenticate(user)
    query = {
        "starts_at_after": "2026-07-01T00:00:00-03:00",
        "starts_at_before": "2026-07-31T23:59:59-03:00",
        **params,
    }
    return api_client.get(URL, query)


def test_conta_por_dia_e_por_status(api_client, manager_single_clinic, agenda):
    response = _get(api_client, manager_single_clinic)
    assert response.status_code == 200

    assert response.data["total"] == 5
    assert response.data["by_day"] == {
        "2026-07-01": 3,
        "2026-07-02": 1,
        "2026-07-03": 1,
    }
    assert response.data["by_status"] == {"scheduled": 4, "canceled": 1}


def test_filtro_por_profissional(api_client, manager_single_clinic, agenda):
    response = _get(api_client, manager_single_clinic, practitioner=agenda["doctor"].pk)

    assert response.data["total"] == 4  # a do "outro" profissional fica de fora
    assert response.data["by_day"] == {
        "2026-07-01": 2,
        "2026-07-02": 1,
        "2026-07-03": 1,
    }


def test_filtro_por_status_aceita_lista(api_client, manager_single_clinic, agenda):
    response = _get(api_client, manager_single_clinic, status="scheduled")

    assert response.data["total"] == 4
    assert "2026-07-03" not in response.data["by_day"]  # a cancelada saiu


def test_janela_recorta_o_periodo(api_client, manager_single_clinic, agenda):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(
        URL,
        {
            "starts_at_after": "2026-07-02T00:00:00-03:00",
            "starts_at_before": "2026-07-02T23:59:59-03:00",
        },
    )
    assert response.data["total"] == 1
    assert list(response.data["by_day"]) == ["2026-07-02"]


def test_o_dia_e_o_do_fuso_da_clinica(api_client, manager_single_clinic, clinic_a, agenda):
    """
    Consulta às 22h de Fortaleza é 01h do dia seguinte em UTC. Se a agregação
    usasse UTC, o calendário mostraria a consulta um dia à frente da lista.
    """
    Appointment.objects.create(
        clinic=clinic_a,
        patient=agenda["appointments"][0].patient,
        practitioner=agenda["doctor"],
        starts_at=datetime(2026, 7, 10, 22, tzinfo=FORTALEZA),
        status=AppointmentStatus.SCHEDULED,
    )

    response = _get(api_client, manager_single_clinic)
    assert response.data["by_day"]["2026-07-10"] == 1
    assert "2026-07-11" not in response.data["by_day"]


def test_escopo_por_clinica(api_client, manager_two_clinics, clinic_a, clinic_b, agenda):
    """A clínica ativa manda: o resumo não enxerga a agenda da outra."""
    patient_b = Patient.objects.create(clinic=clinic_b, name="Paciente Beta")
    Appointment.objects.create(
        clinic=clinic_b,
        patient=patient_b,
        practitioner=Practitioner.objects.create(clinic=clinic_b, name="Dr. Beta"),
        starts_at=datetime(2026, 7, 1, 8, tzinfo=FORTALEZA),
        status=AppointmentStatus.SCHEDULED,
    )

    api_client.force_authenticate(manager_two_clinics)
    response = api_client.get(
        URL,
        {
            "starts_at_after": "2026-07-01T00:00:00-03:00",
            "starts_at_before": "2026-07-31T23:59:59-03:00",
        },
        headers={"X-Clinic-Id": str(clinic_a.pk)},
    )
    assert response.data["total"] == 5  # só as da clínica A

    response_b = api_client.get(
        URL,
        {
            "starts_at_after": "2026-07-01T00:00:00-03:00",
            "starts_at_before": "2026-07-31T23:59:59-03:00",
        },
        headers={"X-Clinic-Id": str(clinic_b.pk)},
    )
    assert response_b.data["total"] == 1


def test_total_bate_com_a_listagem(api_client, manager_single_clinic, agenda):
    """A contagem e a lista precisam contar a MESMA coisa - é o que garante
    que o KPI do mês não divirja do que a agenda mostra."""
    api_client.force_authenticate(manager_single_clinic)
    params = {
        "starts_at_after": "2026-07-01T00:00:00-03:00",
        "starts_at_before": "2026-07-31T23:59:59-03:00",
    }
    summary = api_client.get(URL, params)
    listing = api_client.get("/api/v1/appointments/", params)

    assert summary.data["total"] == listing.data["count"]


def test_agenda_vazia_devolve_zeros(api_client, manager_single_clinic, clinic_a):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(
        URL,
        {
            "starts_at_after": "2030-01-01T00:00:00-03:00",
            "starts_at_before": "2030-01-31T23:59:59-03:00",
        },
    )
    assert response.status_code == 200
    assert response.data == {"total": 0, "by_day": {}, "by_status": {}}


def test_soft_delete_nao_entra_na_contagem(api_client, manager_single_clinic, agenda):
    agenda["appointments"][0].delete()  # soft delete (BaseModel)

    response = _get(api_client, manager_single_clinic)
    assert response.data["total"] == 4
    assert response.data["by_day"]["2026-07-01"] == 2


def test_anonimo_nao_acessa(api_client):
    assert api_client.get(URL).status_code in (401, 403)


def test_consulta_de_hoje_aparece_sem_janela(api_client, manager_single_clinic, clinic_a):
    """Sem filtro de data o resumo cobre tudo — é o que a tela usa ao abrir."""
    patient = Patient.objects.create(clinic=clinic_a, name="Paciente Hoje")
    Appointment.objects.create(
        clinic=clinic_a,
        patient=patient,
        practitioner=Practitioner.objects.create(clinic=clinic_a, name="Dra. Hoje"),
        starts_at=timezone.now() + timedelta(hours=2),
        status=AppointmentStatus.CONFIRMED,
    )

    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL)
    assert response.data["total"] == 1
    assert response.data["by_status"] == {"confirmed": 1}
