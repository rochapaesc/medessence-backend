"""API de pacientes (RF-PAC-1..7) - escopo, filtros, status calculado e tags."""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.core.models import AuditLog
from apps.patients.choices import TagOrigin
from apps.patients.models import Patient, PatientTag, Tag

URL = "/api/v1/patients/"


@pytest.fixture
def patients_a(clinic_a):
    """RF-PAC-2 (90 dias): ativo ≤ 90d; inativo > 90d ou nunca consultou."""
    now = timezone.now()
    active = Patient.objects.create(
        clinic=clinic_a,
        name="Ana Ativa",
        city="Fortaleza",
        last_appointment_at=now - timedelta(days=80),
    )
    inactive = Patient.objects.create(
        clinic=clinic_a,
        name="Ivo Inativo",
        city="Sobral",
        last_appointment_at=now - timedelta(days=120),
    )
    old_inactive = Patient.objects.create(
        clinic=clinic_a,
        name="Rita Retorno",
        cpf="123.456.789-00",
        last_appointment_at=now - timedelta(days=300),
    )
    never = Patient.objects.create(clinic=clinic_a, name="Nino Novo")
    return {"active": active, "inactive": inactive, "old_inactive": old_inactive, "never": never}


@pytest.fixture
def patient_b(clinic_b):
    return Patient.objects.create(clinic=clinic_b, name="Bruno DaOutra")


def test_lista_escopada_na_clinica_ativa(api_client, manager_single_clinic, patients_a, patient_b):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL)
    assert response.status_code == 200
    names = [item["name"] for item in response.data["results"]]
    assert "Bruno DaOutra" not in names  # isolamento (RNF-1)
    assert len(names) == 4


def test_busca_por_nome_e_cpf(api_client, manager_single_clinic, patients_a):
    api_client.force_authenticate(manager_single_clinic)
    by_name = api_client.get(URL, {"search": "rita"})
    assert [i["name"] for i in by_name.data["results"]] == ["Rita Retorno"]
    by_cpf = api_client.get(URL, {"search": "123.456"})
    assert [i["name"] for i in by_cpf.data["results"]] == ["Rita Retorno"]


def test_filtro_por_status_calculado(api_client, manager_single_clinic, patients_a):
    api_client.force_authenticate(manager_single_clinic)

    active = api_client.get(URL, {"status": "active"})
    assert [i["name"] for i in active.data["results"]] == ["Ana Ativa"]

    inactive = api_client.get(URL, {"status": "inactive"})
    assert sorted(i["name"] for i in inactive.data["results"]) == [
        "Ivo Inativo",
        "Nino Novo",
        "Rita Retorno",
    ]


def test_counters_por_status(api_client, manager_single_clinic, patients_a):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(f"{URL}counters/")
    assert response.status_code == 200
    # 3 inativos, mas só 2 já vieram antes (o "novo" sem histórico fica de fora).
    assert response.data == {"total": 4, "active": 1, "inactive": 3, "to_reactivate": 2}


def test_filtro_por_cidade(api_client, manager_single_clinic, patients_a):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL, {"city": "fortaleza"})
    assert [i["name"] for i in response.data["results"]] == ["Ana Ativa"]


def test_create_injeta_clinica_ativa_e_audita(api_client, manager_single_clinic, clinic_a):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(URL, {"name": "Paciente Novo"}, format="json")
    assert response.status_code == 201
    patient = Patient.objects.get(pk=response.data["id"])
    assert patient.clinic_id == clinic_a.pk  # injetada do contexto, não do payload
    assert AuditLog.objects.filter(action="CREATE", resource="Patient", clinic=clinic_a).exists()


def test_tag_ids_sincroniza_atribuicoes_locais(api_client, manager_single_clinic, clinic_a):
    vip = Tag.objects.create(clinic=clinic_a, name="VIP")
    pos = Tag.objects.create(clinic=clinic_a, name="Pós-op")
    api_client.force_authenticate(manager_single_clinic)

    created = api_client.post(URL, {"name": "Com Tags", "tag_ids": [vip.pk, pos.pk]}, format="json")
    assert created.status_code == 201
    patient = Patient.objects.get(pk=created.data["id"])
    assert patient.patient_tags.count() == 2
    assert set(patient.patient_tags.values_list("origin", flat=True)) == {TagOrigin.LOCAL}

    # Atualizar removendo uma tag → soft delete da atribuição
    updated = api_client.patch(f"{URL}{patient.pk}/", {"tag_ids": [vip.pk]}, format="json")
    assert updated.status_code == 200
    assert list(patient.patient_tags.values_list("tag_id", flat=True)) == [vip.pk]
    assert PatientTag.all_objects.filter(patient=patient).count() == 2  # histórico preservado


def test_tag_de_outra_clinica_e_rejeitada(api_client, manager_two_clinics, clinic_a, clinic_b):
    tag_b = Tag.objects.create(clinic=clinic_b, name="Alheia")
    api_client.force_authenticate(manager_two_clinics)
    response = api_client.post(
        URL,
        {"name": "Paciente", "tag_ids": [tag_b.pk]},
        format="json",
        HTTP_X_CLINIC_ID=str(clinic_a.pk),
    )
    assert response.status_code == 400  # nunca confiar em id vindo do cliente


def test_retrieve_de_paciente_de_outra_clinica_retorna_404(
    api_client, manager_single_clinic, patient_b
):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(f"{URL}{patient_b.pk}/")
    assert response.status_code == 404
