"""
Adapter vSaúde — normalizações sobre o contrato REAL (docs/vsaude-swagger.json
+ payloads observados na API em 09/07/2026) e tratamento do envelope ABP.
"""

from unittest.mock import MagicMock, patch

import pytest

from apps.integrations.ehr.exceptions import EHRAuthError, EHRError
from apps.integrations.ehr.vsaude.adapter import VSaudeAdapter
from apps.integrations.ehr.vsaude.client import VSaudeClient
from apps.patients.choices import Gender
from apps.tenants.choices import EHRProviderKind
from apps.tenants.models import Clinic

# Shape real do PatientService/GetAll (chaves observadas na API)
PATIENT_PAYLOAD = {
    "id": "a1b2c3d4-0000-0000-0000-000000000001",
    "name": "  Maria   da Silva ",
    "personalIdentifier": "123.456.789-00",
    "birthday": "1990-05-10T10:00:00Z",
    "gender": 2,
    "email": "maria@exemplo.com",
    "phone": "+55 (85) 99999-0000",
    "address": {"city": "Fortaleza", "state": "CE", "neighborhood": "Aldeota"},
    "profession": "Professora",
    "comments": '<p>Obs</p><script>alert("xss")</script>',
    "insurance": {"id": 5894, "name": "Unimed", "isCompany": False},
    "tags": [1, 4],  # lista de identifiers (não bitmask)
    "status": 1,
}

# Shape real do ScheduleService/GetAll
APPOINTMENT_PAYLOAD = {
    "id": "b1b2c3d4-0000-0000-0000-000000000009",
    "discriminator": "MedicalAppointment",
    "date": "2026-07-09T12:00:00Z",
    "duration": 180,
    "doctor": {"id": "doc-1", "name": " Dr.  House ", "licenceNumber": "CRM123", "userId": 7},
    "patient": {"id": "a1b2c3d4-0000-0000-0000-000000000001", "name": "Maria"},
    "careUnit": {"id": 4540, "name": "Matriz"},
    "procedure": {"id": 36247, "name": "Consulta"},
    "insuranceCompany": {"id": 5894, "name": "Unimed"},
    "insurancePlan": None,
    "status": 81,
    "remotely": False,
}


@pytest.fixture
def clinic(db):
    return Clinic.objects.create(
        name="Clínica vSaúde",
        slug="clinica-vsaude",
        ehr_provider=EHRProviderKind.VSAUDE,
        ehr_credentials={"api_key": "chave-teste", "base_url": "https://vsaude.test/api"},
    )


class DummyClient:
    def __init__(self, result=None):
        self.result = result

    def get(self, path, params=None):
        return self.result

    def post(self, path, body=None):
        return self.result

    def post_paginated(self, path, body=None):
        yield from self.result or []


def test_normalizacao_de_paciente(clinic):
    adapter = VSaudeAdapter(clinic, client=DummyClient())
    patient = adapter._normalize_patient(PATIENT_PAYLOAD)

    assert patient.name == "Maria da Silva"  # trim + colapso de espaços
    assert patient.cpf == "123.456.789-00"  # personalIdentifier
    assert patient.gender == Gender.FEMALE  # int 2 → choice
    assert patient.phone == "5585999990000"  # "+55 (85)..." → E.164 sem "+"
    assert patient.birth_date.isoformat() == "1990-05-10"  # birthday
    assert patient.city == "Fortaleza" and patient.state == "CE"  # do address
    assert patient.insurance_name == "Unimed"
    assert "<script>" not in patient.comments_html  # sanitizado
    assert "<p>Obs</p>" in patient.comments_html
    assert patient.tags_bitmask == 5  # [1, 4] → OR
    assert patient.raw == PATIENT_PAYLOAD  # cru preservado para auditoria


def test_normalizacao_de_consulta(clinic):
    adapter = VSaudeAdapter(clinic, client=DummyClient())
    appointment = adapter._normalize_appointment(APPOINTMENT_PAYLOAD)

    assert appointment.practitioner_name == "Dr. House"
    assert appointment.practitioner_license == "CRM123"
    assert appointment.duration_min == 180
    assert appointment.source_status == "81"
    assert appointment.care_unit_external_id == "4540"
    assert appointment.care_unit_name == "Matriz"
    assert appointment.insurance_plan_external_id == ""  # None → vazio
    assert appointment.starts_at.isoformat().startswith("2026-07-09T12:00")


def test_list_patients_via_post_paginado(clinic):
    adapter = VSaudeAdapter(
        clinic, client=DummyClient({"totalCount": 1, "items": [PATIENT_PAYLOAD]})
    )
    page = adapter.list_patients(page=1)
    assert page.total_count == 1
    assert page.items[0].name == "Maria da Silva"


def test_list_appointments_filtra_discriminator_e_janela(clinic):
    other = {**APPOINTMENT_PAYLOAD, "id": "outro", "discriminator": "Reminder"}
    adapter = VSaudeAdapter(clinic, client=DummyClient([APPOINTMENT_PAYLOAD, other]))
    from datetime import date

    items = adapter.list_appointments(date(2026, 7, 1), date(2026, 7, 31))
    assert len(items) == 1  # só MedicalAppointment
    assert items[0].external_id == APPOINTMENT_PAYLOAD["id"]


def _client_with_response(clinic, status_code=200, json_data=None):
    client = VSaudeClient(clinic)
    response = MagicMock(status_code=status_code)
    response.json.return_value = json_data
    response.text = str(json_data)
    return client, response


def test_envelope_com_success_false_vira_ehrerror(clinic):
    client, response = _client_with_response(
        clinic, json_data={"success": False, "error": {"message": "Falhou feio"}}
    )
    with patch.object(client.session, "request", return_value=response):
        with pytest.raises(EHRError, match="Falhou feio"):
            client.post("PatientService/GetAll")


def test_envelope_so_com_result_e_aberto(clinic):
    """A API real nem sempre manda `success` — `result` presente basta."""
    client, response = _client_with_response(
        clinic, json_data={"result": [{"name": "AA"}], "__abp": True}
    )
    with patch.object(client.session, "request", return_value=response):
        assert client.get("PatientService/GetTags") == [{"name": "AA"}]


def test_401_vira_autherror(clinic):
    client, response = _client_with_response(clinic, status_code=401)
    with patch.object(client.session, "request", return_value=response):
        with pytest.raises(EHRAuthError):
            client.post("PatientService/GetAll")
