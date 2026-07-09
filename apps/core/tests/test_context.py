"""
Testes do contexto de clínica ativa (§3.1) e do isolamento por tenant (RNF-1),
exercitados pelo endpoint escopado real: /api/v1/core/audit-logs/.
"""

import pytest

from apps.core.models import AuditLog

URL = "/api/v1/core/audit-logs/"


@pytest.fixture
def logs_in_both_clinics(clinic_a, clinic_b):
    log_a = AuditLog.objects.create(
        clinic=clinic_a, action="CREATE", resource="Coisa", resource_id="1"
    )
    log_b = AuditLog.objects.create(
        clinic=clinic_b, action="CREATE", resource="Coisa", resource_id="2"
    )
    AuditLog.objects.create(action="LOGIN", resource="User", resource_id="9")  # global
    return log_a, log_b


def test_sem_header_com_dois_vinculos_retorna_400(api_client, manager_two_clinics):
    api_client.force_authenticate(manager_two_clinics)
    response = api_client.get(URL)
    assert response.status_code == 400
    assert response.data["detail"].code == "clinic_context_required"


def test_header_escopa_o_queryset_na_clinica_ativa(
    api_client, manager_two_clinics, clinic_a, logs_in_both_clinics
):
    log_a, log_b = logs_in_both_clinics
    api_client.force_authenticate(manager_two_clinics)
    response = api_client.get(URL, HTTP_X_CLINIC_ID=str(clinic_a.pk))
    assert response.status_code == 200
    ids = [item["id"] for item in response.data["results"]]
    assert log_a.pk in ids
    assert log_b.pk not in ids  # isolamento entre tenants (RNF-1)


def test_logs_globais_nao_vazam_para_o_plano_clinica(
    api_client, manager_single_clinic, clinic_a, logs_in_both_clinics
):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL)
    assert response.status_code == 200
    resources = [item["resource"] for item in response.data["results"]]
    assert "User" not in resources  # o LOGIN global (clinic nula) fica de fora


def test_header_de_clinica_sem_vinculo_retorna_403(api_client, manager_single_clinic, clinic_b):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL, HTTP_X_CLINIC_ID=str(clinic_b.pk))
    assert response.status_code == 403


def test_header_invalido_retorna_400(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL, HTTP_X_CLINIC_ID="abc")
    assert response.status_code == 400


def test_vinculo_unico_auto_resolve_sem_header(
    api_client, manager_single_clinic, clinic_a, logs_in_both_clinics
):
    log_a, _ = logs_in_both_clinics
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL)
    assert response.status_code == 200
    assert [item["id"] for item in response.data["results"]] == [log_a.pk]


def test_atendente_nao_acessa_auditoria(api_client, attendant_a):
    api_client.force_authenticate(attendant_a)
    response = api_client.get(URL)
    assert response.status_code == 403


def test_vinculo_inativo_nao_da_acesso(api_client, manager_single_clinic, clinic_a):
    manager_single_clinic.memberships.update(is_active=False)
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL, HTTP_X_CLINIC_ID=str(clinic_a.pk))
    assert response.status_code == 403


def test_clinica_soft_deletada_sai_da_resolucao(
    api_client, manager_two_clinics, clinic_a, clinic_b, logs_in_both_clinics
):
    """Com a clínica A deletada, o gestor volta a ter UM vínculo válido → auto-resolve B."""
    _, log_b = logs_in_both_clinics
    clinic_a.delete()  # soft delete
    api_client.force_authenticate(manager_two_clinics)
    response = api_client.get(URL)
    assert response.status_code == 200
    assert [item["id"] for item in response.data["results"]] == [log_b.pk]


def test_sem_autenticacao_retorna_401(api_client):
    response = api_client.get(URL)
    assert response.status_code == 401
