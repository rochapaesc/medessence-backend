"""
Linha do tempo clínica (etapa 1 - leitura EHR + CRUD local):
espelho read-only, CRUD local standalone, pull sob demanda e disponibilidade.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.integrations.services import pull_catalogs, pull_medical_records
from apps.patients.models import (
    ClinicalEntry,
    ClinicalOrigin,
    Patient,
    PrescriptionModel,
)
from apps.scheduling.models import Appointment, CareUnit, Practitioner, Procedure
from apps.tenants.choices import EHRProviderKind
from apps.tenants.models import Clinic

ENTRIES_URL = "/api/v1/clinical-entries/"
MODELS_URL = "/api/v1/prescription-models/"


@pytest.fixture
def ehr_clinic(db):
    return Clinic.objects.create(
        name="Clínica EHR", slug="clinica-ehr-clinico", ehr_provider=EHRProviderKind.FAKE
    )


@pytest.fixture
def ehr_client(db, ehr_clinic):
    from conftest import make_user

    user = make_user("clinico.ehr@teste.dev")
    Membership.objects.create(user=user, clinic=ehr_clinic, role="manager")
    client = APIClient()
    client.force_authenticate(user)
    return client


# ------------------------- CRUD local (standalone) --------------------- #


def test_crud_local_de_nota(api_client, manager_single_clinic, clinic_a):
    api_client.force_authenticate(manager_single_clinic)
    patient = Patient.objects.create(clinic=clinic_a, name="Paciente Nota")

    created = api_client.post(
        ENTRIES_URL,
        {
            "patient": patient.pk,
            "kind": "note",
            "date": timezone.now().isoformat(),
            "text": '<p>Evolução</p><script>alert("x")</script>',
        },
        format="json",
    )
    assert created.status_code == 201
    entry = ClinicalEntry.objects.get(pk=created.data["id"])
    assert entry.origin == ClinicalOrigin.LOCAL
    assert "<script>" not in entry.text  # sanitizado

    updated = api_client.patch(
        f"{ENTRIES_URL}{entry.pk}/", {"text": "<p>Editada</p>"}, format="json"
    )
    assert updated.status_code == 200

    listed = api_client.get(ENTRIES_URL, {"patient": patient.pk})
    assert listed.data["count"] == 1

    deleted = api_client.delete(f"{ENTRIES_URL}{entry.pk}/")
    assert deleted.status_code == 204


def test_nota_sem_texto_e_exame_sem_titulo_sao_rejeitados(
    api_client, manager_single_clinic, clinic_a
):
    api_client.force_authenticate(manager_single_clinic)
    patient = Patient.objects.create(clinic=clinic_a, name="Paciente Valida")
    now = timezone.now().isoformat()

    nota = api_client.post(
        ENTRIES_URL, {"patient": patient.pk, "kind": "note", "date": now}, format="json"
    )
    assert nota.status_code == 400
    exame = api_client.post(
        ENTRIES_URL, {"patient": patient.pk, "kind": "exam", "date": now}, format="json"
    )
    assert exame.status_code == 400


def test_espelho_ehr_e_read_only(api_client, manager_single_clinic, clinic_a):
    api_client.force_authenticate(manager_single_clinic)
    patient = Patient.objects.create(clinic=clinic_a, name="Paciente Espelho")
    entry = ClinicalEntry.objects.create(
        clinic=clinic_a,
        patient=patient,
        kind="note",
        origin=ClinicalOrigin.EHR,
        date=timezone.now(),
        text="<p>Do EHR</p>",
        source="record",
        external_id="mre-1",
    )
    assert (
        api_client.patch(f"{ENTRIES_URL}{entry.pk}/", {"text": "x"}, format="json").status_code
        == 403
    )
    assert api_client.delete(f"{ENTRIES_URL}{entry.pk}/").status_code == 403


# ----------------------- pull do espelho (fake EHR) --------------------- #


def test_pull_medical_records_sob_demanda(ehr_clinic):
    patient = Patient.objects.create(
        clinic=ehr_clinic, name="Paciente Clínico", external_id="fake-pat-clin-1"
    )
    Practitioner.objects.create(clinic=ehr_clinic, name="Dr(a). Fake 1", external_id="fake-prof-1")

    run = pull_medical_records(ehr_clinic, patient=patient)
    assert run.stats == {"patients": 1, "fetched": 5, "created": 5, "updated": 0}

    entries = ClinicalEntry.objects.filter(patient=patient)
    kinds = sorted(entries.values_list("kind", flat=True))
    assert kinds == ["exam", "exam", "form_response", "note", "prescription"]
    assert set(entries.values_list("origin", flat=True)) == {ClinicalOrigin.EHR}
    # creator GUID casa com o Practitioner espelhado
    note = entries.get(kind="note")
    assert note.practitioner is not None and note.practitioner.external_id == "fake-prof-1"

    # Idempotente
    run2 = pull_medical_records(ehr_clinic, patient=patient)
    assert run2.stats["created"] == 0 and run2.stats["updated"] == 5


def test_sync_action_puxa_prontuario(ehr_client, ehr_clinic):
    patient = Patient.objects.create(
        clinic=ehr_clinic, name="Paciente Sync", external_id="fake-pat-clin-2"
    )
    response = ehr_client.post(f"{ENTRIES_URL}sync/", {"patient": patient.pk}, format="json")
    assert response.status_code == 200
    assert response.data["synced"] is True
    assert ClinicalEntry.objects.filter(patient=patient).count() == 5


def test_pull_catalogs_traz_modelos_de_prescricao(ehr_clinic):
    run = pull_catalogs(ehr_clinic)
    assert run.stats["prescription_models"]["fetched"] == 2
    model = PrescriptionModel.objects.get(clinic=ehr_clinic, external_id="fake-pm-1")
    assert model.origin == ClinicalOrigin.EHR


def test_modelo_ehr_read_only_local_editavel(ehr_client, ehr_clinic):
    pull_catalogs(ehr_clinic)
    mirrored = PrescriptionModel.objects.filter(clinic=ehr_clinic).first()
    assert (
        ehr_client.patch(f"{MODELS_URL}{mirrored.pk}/", {"name": "x"}, format="json").status_code
        == 403
    )

    created = ehr_client.post(
        MODELS_URL, {"name": "Modelo Local", "content": "<p>ok</p>"}, format="json"
    )
    assert created.status_code == 201
    local = PrescriptionModel.objects.get(pk=created.data["id"])
    assert local.origin == ClinicalOrigin.LOCAL
    assert ehr_client.patch(
        f"{MODELS_URL}{local.pk}/", {"name": "Modelo Local 2"}, format="json"
    ).status_code == 200


# --------------------------- disponibilidade ---------------------------- #


def test_availability_local_deriva_do_work_journey(api_client, manager_single_clinic, clinic_a):
    api_client.force_authenticate(manager_single_clinic)
    day = (timezone.localtime() + timedelta(days=1)).replace(
        hour=9, minute=0, second=0, microsecond=0
    )
    practitioner = Practitioner.objects.create(clinic=clinic_a, name="Dra. Local")
    procedure = Procedure.objects.create(clinic=clinic_a, name="Consulta", duration_min=30)
    unit = CareUnit.objects.create(
        clinic=clinic_a,
        name="Sede",
        work_journey=[
            {
                "startDate": day.isoformat(),
                "endDate": (day + timedelta(hours=2)).isoformat(),
                "available": True,
            }
        ],
    )
    # Consulta local ocupa 09:30-10:00
    patient = Patient.objects.create(clinic=clinic_a, name="Paciente Slot")
    Appointment.objects.create(
        clinic=clinic_a,
        patient=patient,
        practitioner=practitioner,
        starts_at=day + timedelta(minutes=30),
        duration_min=30,
    )

    response = api_client.get(
        f"/api/v1/practitioners/{practitioner.pk}/availability/",
        {"date": day.date().isoformat(), "procedure": procedure.pk, "care_unit": unit.pk},
    )
    assert response.status_code == 200
    assert response.data["source"] == "local"
    assert response.data["times"] == ["09:00", "10:00", "10:30"]  # 09:30 ocupado


def test_availability_com_ehr_usa_o_provider(ehr_client, ehr_clinic):
    practitioner = Practitioner.objects.create(
        clinic=ehr_clinic, name="Dr(a). Fake 1", external_id="fake-prof-1"
    )
    response = ehr_client.get(
        f"/api/v1/practitioners/{practitioner.pk}/availability/",
        {"date": "2026-08-05"},
    )
    assert response.status_code == 200
    assert response.data["source"] == "ehr"
    assert response.data["times"] == ["09:00", "09:30", "10:00", "14:00", "14:30"]


# ------------------- paciente com campos ampliados ---------------------- #


def test_paciente_com_campos_ampliados(api_client, manager_single_clinic, clinic_a):
    api_client.force_authenticate(manager_single_clinic)
    created = api_client.post(
        "/api/v1/patients/",
        {
            "name": "Paciente Completo",
            "blood_type": "O-",
            "weight_kg": "82.5",
            "height_cm": "178.0",
            "guardians": {"mother": {"name": "Mãe Teste", "phone": "5585999990000"}},
            "birth_info": {"gestationalAge": 39},
            "address": {"postalCode": "60000000", "neighborhood": "Centro", "number": "10"},
        },
        format="json",
    )
    assert created.status_code == 201
    detail = api_client.get(f"/api/v1/patients/{created.data['id']}/")
    assert detail.data["blood_type"] == "O-"
    assert detail.data["weight_kg"] == "82.5"
    assert detail.data["guardians"]["mother"]["name"] == "Mãe Teste"
    assert detail.data["address"]["neighborhood"] == "Centro"
