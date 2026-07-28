"""
Write-through nós → EHR (§10.2) com o provider FAKE: padrão único de
escrita (API REST → local → SyncOperation → adapter), dedupe por CPF,
tags por diff, transições semânticas e delete bidirecional.
"""

from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.accounts.models import Membership
from apps.core.choices import SyncStatus
from apps.integrations.choices import OperationStatus
from apps.integrations.ehr.fake.adapter import FakeAdapter
from apps.integrations.models import SyncOperation
from apps.integrations.push import process_clinic_operations
from apps.integrations.services import pull_patients
from apps.patients.choices import PatientSource
from apps.patients.models import Patient, Tag
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import (
    Appointment,
    CareUnit,
    EHRStatusMap,
    InsuranceCompany,
    Practitioner,
    PractitionerProcedure,
    Procedure,
)
from apps.tenants.choices import EHRProviderKind
from apps.tenants.models import Clinic

PATIENTS_URL = "/api/v1/patients/"
APPOINTMENTS_URL = "/api/v1/appointments/"


@pytest.fixture(autouse=True)
def _sem_trava_de_producao(settings):
    """
    Estes testes existem para provar que o DELETE propaga para o EHR — que é
    exatamente o que a trava `EHR_DATA_GUARD` bloqueia (apps/core/api/guards.py,
    28/07/2026). Aqui ela sai do caminho de propósito; a trava tem suíte
    própria em `apps/core/tests/test_ehr_data_guard.py`.
    """
    settings.EHR_DATA_GUARD = False


@pytest.fixture(autouse=True)
def _reset_fake_registries():
    """Os registros de escrita do fake são de CLASSE - zera entre testes."""
    for attr in (
        "_created_patients",
        "_deleted_patients",
        "_patient_tags",
        "_extra_tags",
        "_appointments",
        "_sequences",
    ):
        setattr(FakeAdapter, attr, {})


@pytest.fixture
def ehr_clinic(db):
    """
    Clínica com EHR fake. O mapa de status vem da migration 0009 (ciclo do
    pull + códigos das transições), não montado aqui: dublê que inventa o
    contrato esconde defeito real — foi assim que o `unmapped_statuses`
    sobreviveu à P4 (28/07/2026).
    """
    return Clinic.objects.create(
        name="Clínica Integrada",
        slug="clinica-integrada",
        ehr_provider=EHRProviderKind.FAKE,
        ehr_push_enabled=True,  # testes exercitam o write-through
    )


@pytest.fixture
def ehr_manager(db, ehr_clinic):
    from conftest import make_user

    user = make_user("gestor.ehr@teste.dev")
    Membership.objects.create(user=user, clinic=ehr_clinic, role="manager")
    return user


@pytest.fixture
def client_ehr(ehr_manager):
    client = APIClient()
    client.force_authenticate(ehr_manager)
    return client


@pytest.fixture
def scheduling_setup(ehr_clinic):
    """Catálogos mínimos p/ criar consulta (com preço por profissional)."""
    practitioner = Practitioner.objects.create(
        clinic=ehr_clinic, name="Dr(a). Fake 1", external_id="fake-prof-1"
    )
    procedure = Procedure.objects.create(
        clinic=ehr_clinic, name="Consulta", external_id="fake-proc-1", duration_min=30
    )
    care_unit = CareUnit.objects.create(
        clinic=ehr_clinic, name="Matriz", external_id="fake-unit-1"
    )
    insurance = InsuranceCompany.objects.create(
        clinic=ehr_clinic, name="Particular", external_id="fake-ins-1"
    )
    PractitionerProcedure.objects.create(
        clinic=ehr_clinic,
        practitioner=practitioner,
        procedure=procedure,
        duration_min=30,
        price="400.00",
    )
    patient = Patient.objects.create(
        clinic=ehr_clinic, name="Paciente Sincronizado", external_id="fake-pat-ok"
    )
    return {
        "practitioner": practitioner,
        "procedure": procedure,
        "care_unit": care_unit,
        "insurance": insurance,
        "patient": patient,
    }


# ----------------------------- standalone ----------------------------- #


def test_clinica_sem_ehr_nao_enfileira_nada(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(PATIENTS_URL, {"name": "Paciente Standalone"}, format="json")
    assert response.status_code == 201
    patient = Patient.objects.get(pk=response.data["id"])
    assert patient.sync_status == SyncStatus.SYNCED  # sem selo pendente
    assert SyncOperation.objects.count() == 0


# ------------------------- pacientes (push) --------------------------- #


def test_create_paciente_empurra_e_grava_external_id(client_ehr, ehr_clinic):
    response = client_ehr.post(PATIENTS_URL, {"name": "Paciente Novo Push"}, format="json")
    assert response.status_code == 201
    patient = Patient.objects.get(pk=response.data["id"])
    assert patient.sync_status == SyncStatus.PENDING  # selo "aguardando"

    stats = process_clinic_operations(ehr_clinic)
    assert stats["succeeded"] == 1

    patient.refresh_from_db()
    assert patient.external_id.startswith("fake-")
    assert patient.sync_status == SyncStatus.SYNCED


def test_create_com_cpf_existente_vincula_sem_duplicar(client_ehr, ehr_clinic):
    """Dedupe (§10.2): CPF já existe no EHR → vincula em vez de criar."""
    existing = FakeAdapter(ehr_clinic)._patients[0]
    response = client_ehr.post(
        PATIENTS_URL, {"name": "Homônimo Local", "cpf": existing.cpf}, format="json"
    )
    assert response.status_code == 201

    process_clinic_operations(ehr_clinic)
    patient = Patient.objects.get(pk=response.data["id"])
    assert patient.external_id == existing.external_id  # vinculado, não criado


def test_update_e_delete_de_paciente_geram_push(client_ehr, ehr_clinic):
    created = client_ehr.post(PATIENTS_URL, {"name": "Paciente Ciclo"}, format="json")
    process_clinic_operations(ehr_clinic)
    pk = created.data["id"]

    updated = client_ehr.patch(f"{PATIENTS_URL}{pk}/", {"name": "Paciente Editado"}, format="json")
    assert updated.status_code == 200
    deleted = client_ehr.delete(f"{PATIENTS_URL}{pk}/")
    assert deleted.status_code == 204

    stats = process_clinic_operations(ehr_clinic)
    assert stats["failed"] == 0
    patient = Patient.all_objects.get(pk=pk)
    assert patient.deleted_at is not None  # soft local
    external_id = patient.external_id
    assert external_id in FakeAdapter._deleted_patients[ehr_clinic.pk]  # soft no EHR


def test_tags_por_diff_promovem_tag_local(client_ehr, ehr_clinic):
    tag = Tag.objects.create(clinic=ehr_clinic, name="Academia")
    created = client_ehr.post(
        PATIENTS_URL, {"name": "Paciente Taggeado", "tag_ids": [tag.pk]}, format="json"
    )
    assert created.status_code == 201

    stats = process_clinic_operations(ehr_clinic)
    assert stats["failed"] == 0

    tag.refresh_from_db()
    assert tag.identifier  # AddTag devolveu identifier → promovida
    patient = Patient.objects.get(pk=created.data["id"])
    assert "Academia" in FakeAdapter._patient_tags[ehr_clinic.pk][patient.external_id]


# --------------------------- agenda (push) ---------------------------- #


def _create_appointment(client, setup, **extra):
    payload = {
        "patient": setup["patient"].pk,
        "practitioner": setup["practitioner"].pk,
        "procedure": setup["procedure"].pk,
        "care_unit": setup["care_unit"].pk,
        "insurance_company": setup["insurance"].pk,
        "starts_at": (timezone.now() + timedelta(days=3)).isoformat(),
        "duration_min": 30,
        **extra,
    }
    return client.post(APPOINTMENTS_URL, payload, format="json")


def test_create_consulta_write_through(client_ehr, ehr_clinic, scheduling_setup):
    response = _create_appointment(client_ehr, scheduling_setup)
    assert response.status_code == 201

    appointment = Appointment.objects.get(pk=response.data["id"])
    assert appointment.sync_status == SyncStatus.PENDING

    stats = process_clinic_operations(ehr_clinic)
    assert stats["succeeded"] == 1

    appointment.refresh_from_db()
    assert appointment.external_id.startswith("fake-")
    assert appointment.source_status == "10"  # agendada
    assert appointment.sync_status == SyncStatus.SYNCED


def test_transicao_por_patch_aciona_rota_e_confirma_codigo(
    client_ehr, ehr_clinic, scheduling_setup
):
    """PATCH {status} → ação no EHR → código confirmado por re-fetch."""
    created = _create_appointment(client_ehr, scheduling_setup)
    process_clinic_operations(ehr_clinic)
    pk = created.data["id"]

    response = client_ehr.patch(
        f"{APPOINTMENTS_URL}{pk}/", {"status": "completed"}, format="json"
    )
    assert response.status_code == 200
    stats = process_clinic_operations(ehr_clinic)
    assert stats["failed"] == 0

    appointment = Appointment.objects.get(pk=pk)
    assert appointment.status == AppointmentStatus.COMPLETED
    assert appointment.source_status == "81"  # código gravado pelo EHR


def test_editar_em_lugar_gera_update_nao_transicao(client_ehr, ehr_clinic, scheduling_setup):
    """Editar (mudar a data EM-LUGAR) → op update no MESMO registro. O
    "Remarcar" do produto não passa por aqui: duplica via POST (RF-AGE-5)."""
    created = _create_appointment(client_ehr, scheduling_setup)
    process_clinic_operations(ehr_clinic)
    pk = created.data["id"]

    new_start = (timezone.now() + timedelta(days=5)).isoformat()
    response = client_ehr.patch(
        f"{APPOINTMENTS_URL}{pk}/", {"starts_at": new_start}, format="json"
    )
    assert response.status_code == 200

    operation = SyncOperation.objects.filter(status=OperationStatus.PENDING).get()
    assert operation.payload["op"] == "update"
    assert process_clinic_operations(ehr_clinic)["succeeded"] == 1


def test_nao_troca_paciente_de_consulta_sincronizada(client_ehr, ehr_clinic, scheduling_setup):
    created = _create_appointment(client_ehr, scheduling_setup)
    process_clinic_operations(ehr_clinic)
    other = Patient.objects.create(
        clinic=ehr_clinic, name="Outro Paciente", external_id="fake-pat-2"
    )
    response = client_ehr.patch(
        f"{APPOINTMENTS_URL}{created.data['id']}/", {"patient": other.pk}, format="json"
    )
    assert response.status_code == 400  # regra do EHR espelhada


def test_delete_consulta_propaga(client_ehr, ehr_clinic, scheduling_setup):
    created = _create_appointment(client_ehr, scheduling_setup)
    process_clinic_operations(ehr_clinic)
    pk = created.data["id"]
    external_id = Appointment.objects.get(pk=pk).external_id
    assert external_id in FakeAdapter._appointments[ehr_clinic.pk]

    assert client_ehr.delete(f"{APPOINTMENTS_URL}{pk}/").status_code == 204
    process_clinic_operations(ehr_clinic)
    assert external_id not in FakeAdapter._appointments[ehr_clinic.pk]


def test_remarcar_duplicador_vira_create_novo(client_ehr, ehr_clinic, scheduling_setup):
    """Remarcar DUPLICA (decisão de produto, RF-AGE-5): o segundo POST vira
    Create novo no EHR com external_id PRÓPRIO; a original não é tocada."""
    original = _create_appointment(client_ehr, scheduling_setup)
    process_clinic_operations(ehr_clinic)
    original_ext = Appointment.objects.get(pk=original.data["id"]).external_id

    duplicate = _create_appointment(
        client_ehr,
        scheduling_setup,
        starts_at=(timezone.now() + timedelta(days=9)).isoformat(),
    )
    assert duplicate.status_code == 201
    assert process_clinic_operations(ehr_clinic)["succeeded"] == 1

    dup = Appointment.objects.get(pk=duplicate.data["id"])
    assert dup.external_id and dup.external_id != original_ext

    # Os DOIS registros vivem no EHR - nada foi remarcado em-lugar
    store = FakeAdapter._appointments[ehr_clinic.pk]
    assert original_ext in store and dup.external_id in store
    assert Appointment.objects.get(pk=original.data["id"]).source_status == "10"


def test_transicao_waiting_confirma_codigo_9(client_ehr, ehr_clinic, scheduling_setup):
    """Aguardando atendimento tem rota própria e confirma o código 9."""
    created = _create_appointment(client_ehr, scheduling_setup)
    process_clinic_operations(ehr_clinic)
    pk = created.data["id"]

    client_ehr.patch(f"{APPOINTMENTS_URL}{pk}/", {"status": "waiting"}, format="json")
    assert process_clinic_operations(ehr_clinic)["failed"] == 0

    appointment = Appointment.objects.get(pk=pk)
    assert appointment.status == AppointmentStatus.WAITING
    assert appointment.source_status == "9"


def test_in_progress_e_local_only_sem_regressao(client_ehr, ehr_clinic, scheduling_setup):
    """RF-AGE-5: in_progress não tem rota - a op conclui SEM tocar o EHR e
    sem a confirmação regredir o status otimista local."""
    created = _create_appointment(client_ehr, scheduling_setup)
    process_clinic_operations(ehr_clinic)
    pk = created.data["id"]

    client_ehr.patch(f"{APPOINTMENTS_URL}{pk}/", {"status": "waiting"}, format="json")
    process_clinic_operations(ehr_clinic)

    client_ehr.patch(f"{APPOINTMENTS_URL}{pk}/", {"status": "in_progress"}, format="json")
    assert process_clinic_operations(ehr_clinic)["failed"] == 0

    appointment = Appointment.objects.get(pk=pk)
    assert appointment.status == AppointmentStatus.IN_PROGRESS  # não regrediu
    assert appointment.source_status == "9"  # EHR segue em "aguardando"
    assert appointment.sync_status == SyncStatus.SYNCED
    ehr_record = FakeAdapter._appointments[ehr_clinic.pk][appointment.external_id]
    assert ehr_record.source_status == "9"  # transição sem rota não tocou lá


def test_op_waiting_atrasada_nao_regride_in_progress(client_ehr, ehr_clinic, scheduling_setup):
    """Corrida real: a op de waiting ainda está na fila quando o usuário já
    puxou a consulta para in_progress - a confirmação não pode regredir."""
    created = _create_appointment(client_ehr, scheduling_setup)
    process_clinic_operations(ehr_clinic)
    pk = created.data["id"]

    # Duas transições ANTES de o worker rodar
    client_ehr.patch(f"{APPOINTMENTS_URL}{pk}/", {"status": "waiting"}, format="json")
    client_ehr.patch(f"{APPOINTMENTS_URL}{pk}/", {"status": "in_progress"}, format="json")
    assert process_clinic_operations(ehr_clinic)["failed"] == 0

    appointment = Appointment.objects.get(pk=pk)
    assert appointment.status == AppointmentStatus.IN_PROGRESS
    assert appointment.source_status == "9"  # waiting FOI empurrado ao EHR


def test_consulta_sem_convenio_cai_no_particular(ehr_clinic, scheduling_setup):
    """§10.2: sem convênio → convênio "Particular" do tenant no payload (o
    app web da vSaúde manda o id do Particular, nunca null)."""
    from apps.integrations.push import _appointment_data

    appointment = Appointment.objects.create(
        clinic=ehr_clinic,
        patient=scheduling_setup["patient"],
        practitioner=scheduling_setup["practitioner"],
        procedure=scheduling_setup["procedure"],
        care_unit=scheduling_setup["care_unit"],
        starts_at=timezone.now() + timedelta(days=2),
        duration_min=30,
    )
    data = _appointment_data(appointment)
    assert data["insurance_company_external_id"] == "fake-ins-1"

    # Clínica sem convênio "Particular" no catálogo → segue vazio
    InsuranceCompany.objects.filter(clinic=ehr_clinic).update(name="Unimed")
    data = _appointment_data(appointment)
    assert data["insurance_company_external_id"] == ""


# ------------------- delete bidirecional (EHR → nós) ------------------ #


def test_sweep_remove_espelhado_que_sumiu_do_ehr(ehr_clinic):
    gone = Patient.objects.create(
        clinic=ehr_clinic,
        name="Apagado No EHR",
        source=PatientSource.EHR,
        external_id="fake-gone-123",
    )
    local = Patient.objects.create(clinic=ehr_clinic, name="Só Local", source=PatientSource.LOCAL)

    run = pull_patients(ehr_clinic)
    assert run.stats["removed"] == 1

    gone.refresh_from_db()
    local.refresh_from_db()
    assert gone.deleted_at is not None  # soft delete espelhado
    assert local.deleted_at is None  # local intocado


# ------------------------ resiliência da fila ------------------------- #


def test_erro_transitorio_mantem_pendente(client_ehr, ehr_clinic, monkeypatch):
    from apps.integrations.ehr.exceptions import EHRUnavailableError

    client_ehr.post(PATIENTS_URL, {"name": "Paciente Sem Rede"}, format="json")

    def _down(*args, **kwargs):
        raise EHRUnavailableError("EHR fora do ar")

    monkeypatch.setattr(FakeAdapter, "create_patient", _down)
    stats = process_clinic_operations(ehr_clinic)
    assert stats["deferred"] == 1

    operation = SyncOperation.objects.get()
    assert operation.status == OperationStatus.PENDING  # o beat retoma
    assert operation.attempts == 1

    monkeypatch.undo()
    assert process_clinic_operations(ehr_clinic)["succeeded"] == 1  # recuperou


def test_erro_permanente_marca_failed_no_registro(client_ehr, ehr_clinic, monkeypatch):
    created = client_ehr.post(PATIENTS_URL, {"name": "Paciente Rejeitado"}, format="json")

    def _boom(*args, **kwargs):
        raise ValueError("payload rejeitado")

    monkeypatch.setattr(FakeAdapter, "create_patient", _boom)
    stats = process_clinic_operations(ehr_clinic)
    assert stats["failed"] == 1

    patient = Patient.objects.get(pk=created.data["id"])
    assert patient.sync_status == SyncStatus.FAILED  # visível na UI
    assert SyncOperation.objects.get().status == OperationStatus.FAILED


# ---------------------- catálogos ampliados (pull) --------------------- #


def test_pull_catalogs_traz_profissionais_e_ofertas(ehr_clinic):
    from apps.integrations.services import pull_catalogs

    run = pull_catalogs(ehr_clinic)
    assert run.stats["professionals"]["fetched"] == 2
    assert run.stats["professionals"]["offers"] == 6  # 3 procedimentos x 2

    assert Practitioner.objects.filter(clinic=ehr_clinic).count() == 2
    offer = PractitionerProcedure.objects.filter(
        clinic=ehr_clinic,
        practitioner__external_id="fake-prof-1",
        procedure__external_id="fake-proc-1",
    ).get()
    assert str(offer.price) == "400.00"  # preço por profissional p/ o form


def test_push_desligado_nao_enfileira_mesmo_com_ehr(api_client, ehr_clinic, ehr_manager):
    """Trava da fase só-leitura: provider configurado mas push OFF → local puro."""
    ehr_clinic.ehr_push_enabled = False
    ehr_clinic.save(update_fields=["ehr_push_enabled"])
    api_client.force_authenticate(ehr_manager)

    response = api_client.post(PATIENTS_URL, {"name": "Paciente Só Leitura"}, format="json")
    assert response.status_code == 201
    patient = Patient.objects.get(pk=response.data["id"])
    assert patient.sync_status == SyncStatus.SYNCED  # sem selo pendente
    assert SyncOperation.objects.count() == 0  # nada vai ao EHR
