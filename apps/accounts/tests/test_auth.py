"""Fluxo JWT (SimpleJWT) + /me + /me/memberships."""

from apps.core.models import AuditLog
from conftest import PASSWORD

TOKEN_URL = "/api/v1/auth/token/"
REFRESH_URL = "/api/v1/auth/token/refresh/"
ME_URL = "/api/v1/me/"
MEMBERSHIPS_URL = "/api/v1/me/memberships/"


def test_login_e_acesso_ao_me(api_client, manager_single_clinic):
    response = api_client.post(
        TOKEN_URL,
        {"email": manager_single_clinic.email, "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 200
    assert "access" in response.data and "refresh" in response.data

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
    me = api_client.get(ME_URL)
    assert me.status_code == 200
    assert me.data["email"] == manager_single_clinic.email


def test_login_gera_auditoria(api_client, manager_single_clinic):
    api_client.post(
        TOKEN_URL,
        {"email": manager_single_clinic.email, "password": PASSWORD},
        format="json",
    )
    assert AuditLog.objects.filter(action="LOGIN", user=manager_single_clinic).exists()


def test_login_invalido_retorna_401_e_audita_falha(api_client, manager_single_clinic):
    response = api_client.post(
        TOKEN_URL,
        {"email": manager_single_clinic.email, "password": "senha-errada"},
        format="json",
    )
    assert response.status_code == 401
    assert AuditLog.objects.filter(action="LOGIN_FAILED").exists()


def test_refresh_token(api_client, manager_single_clinic):
    tokens = api_client.post(
        TOKEN_URL,
        {"email": manager_single_clinic.email, "password": PASSWORD},
        format="json",
    ).data
    response = api_client.post(REFRESH_URL, {"refresh": tokens["refresh"]}, format="json")
    assert response.status_code == 200
    assert "access" in response.data


def test_memberships_do_usuario_logado(api_client, manager_two_clinics):
    api_client.force_authenticate(manager_two_clinics)
    response = api_client.get(MEMBERSHIPS_URL)
    assert response.status_code == 200
    assert len(response.data) == 2
    slugs = {item["clinic"]["slug"] for item in response.data}
    assert slugs == {"clinica-alfa", "clinica-beta"}
    assert all(item["role"] == "manager" for item in response.data)


def test_memberships_exclui_inativos_e_de_outros_usuarios(
    api_client, manager_two_clinics, attendant_a
):
    manager_two_clinics.memberships.filter(clinic__slug="clinica-beta").update(is_active=False)
    api_client.force_authenticate(manager_two_clinics)
    response = api_client.get(MEMBERSHIPS_URL)
    assert len(response.data) == 1
    assert response.data[0]["clinic"]["slug"] == "clinica-alfa"


def test_patch_me_atualiza_nome(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.patch(ME_URL, {"first_name": "Novo"}, format="json")
    assert response.status_code == 200
    manager_single_clinic.refresh_from_db()
    assert manager_single_clinic.first_name == "Novo"
