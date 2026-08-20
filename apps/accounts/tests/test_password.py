"""
Troca da própria senha, senha temporária e o que a troca derruba (§4.12:
RF-CTA-2, RF-CTA-3, RF-CTA-5 e RF-EQP-7).
"""

import time
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework_simplejwt.tokens import AccessToken, RefreshToken

from apps.accounts.models import User
from apps.accounts.passwords import generate_temporary_password, set_user_password
from apps.core.models import AuditLog
from conftest import PASSWORD

TOKEN_URL = "/api/v1/auth/token/"
REFRESH_URL = "/api/v1/auth/token/refresh/"
ME_URL = "/api/v1/me/"
PASSWORD_URL = "/api/v1/me/password/"
PATIENTS_URL = "/api/v1/patients/"

NEW_PASSWORD = "trilha-de-pedra-77"


def login(api_client, user, password=PASSWORD):
    response = api_client.post(
        TOKEN_URL, {"email": user.email, "password": password}, format="json"
    )
    assert response.status_code == 200, response.data
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
    return response.data


# --------------------------------------------------------------------------- #
# RF-CTA-2: a senha atual é obrigatória e conferida antes de qualquer escrita
# --------------------------------------------------------------------------- #


def test_troca_a_propria_senha(api_client, manager_single_clinic):
    login(api_client, manager_single_clinic)

    response = api_client.post(
        PASSWORD_URL,
        {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        format="json",
    )

    assert response.status_code == 200
    manager_single_clinic.refresh_from_db()
    assert manager_single_clinic.check_password(NEW_PASSWORD)


def test_senha_atual_errada_nao_troca_nada(api_client, manager_single_clinic):
    login(api_client, manager_single_clinic)

    response = api_client.post(
        PASSWORD_URL,
        {"current_password": "nao-e-a-minha-senha", "new_password": NEW_PASSWORD},
        format="json",
    )

    assert response.status_code == 400
    assert "current_password" in response.data
    manager_single_clinic.refresh_from_db()
    assert manager_single_clinic.check_password(PASSWORD)


def test_senha_atual_e_obrigatoria(api_client, manager_single_clinic):
    login(api_client, manager_single_clinic)

    response = api_client.post(PASSWORD_URL, {"new_password": NEW_PASSWORD}, format="json")

    assert response.status_code == 400
    assert "current_password" in response.data


def test_senha_nova_fraca_e_recusada(api_client, manager_single_clinic):
    login(api_client, manager_single_clinic)

    response = api_client.post(
        PASSWORD_URL,
        {"current_password": PASSWORD, "new_password": "123456"},
        format="json",
    )

    assert response.status_code == 400
    assert "new_password" in response.data
    manager_single_clinic.refresh_from_db()
    assert manager_single_clinic.check_password(PASSWORD)


def test_troca_de_senha_deixa_rastro_sem_o_valor(api_client, manager_single_clinic):
    login(api_client, manager_single_clinic)

    api_client.post(
        PASSWORD_URL,
        {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        format="json",
    )

    log = AuditLog.objects.filter(
        action="UPDATE", resource="User", user=manager_single_clinic
    ).first()
    assert log is not None
    assert log.payload == {"changed_fields": ["password"]}
    assert NEW_PASSWORD not in str(log.payload)


# --------------------------------------------------------------------------- #
# RF-CTA-3: a troca derruba as outras sessões, e não a de quem trocou
# --------------------------------------------------------------------------- #


def token_da_aba_de_ontem(user, klass):
    """
    Token emitido uma hora atrás, que é o caso real: a outra aba, o outro
    computador, aberto desde antes da troca de senha.

    ⚠️ Não dá para fazer login e trocar a senha em seguida no teste e esperar
    que o token morra: os dois acontecem no MESMO segundo, e o `iat` do JWT
    só tem resolução de segundo. A folga de um segundo é conhecida e está
    documentada em `token_predates_password_change`.
    """
    token = klass.for_user(user)
    token.payload["iat"] = int(time.time()) - 3600
    return str(token)


def test_token_anterior_a_troca_para_de_valer(api_client, manager_single_clinic):
    antigo = token_da_aba_de_ontem(manager_single_clinic, AccessToken)
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {antigo}")
    assert api_client.get(ME_URL).status_code == 200

    set_user_password(manager_single_clinic, NEW_PASSWORD)

    # O cliente segue com o token ANTIGO no cabeçalho: ele tem de morrer.
    assert api_client.get(ME_URL).status_code == 401


def test_quem_trocou_a_senha_recebe_par_de_tokens_que_funciona(api_client, manager_single_clinic):
    login(api_client, manager_single_clinic)

    response = api_client.post(
        PASSWORD_URL,
        {"current_password": PASSWORD, "new_password": NEW_PASSWORD},
        format="json",
    )

    assert "access" in response.data and "refresh" in response.data
    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {response.data['access']}")
    assert api_client.get(ME_URL).status_code == 200


def test_refresh_anterior_a_troca_nao_renova(api_client, manager_single_clinic):
    antigo = token_da_aba_de_ontem(manager_single_clinic, RefreshToken)

    set_user_password(manager_single_clinic, NEW_PASSWORD)

    # ⚠️ O caminho que realmente mantém a sessão viva: o access morre em 4h,
    # mas o refresh de 30 dias compraria um access NOVO se ninguém olhasse.
    response = api_client.post(REFRESH_URL, {"refresh": antigo}, format="json")
    assert response.status_code == 401


def test_refresh_valido_continua_renovando(api_client, manager_single_clinic):
    """Prova negativa da guarda acima: sem troca de senha, o refresh trabalha."""
    tokens = login(api_client, manager_single_clinic)

    response = api_client.post(REFRESH_URL, {"refresh": tokens["refresh"]}, format="json")

    assert response.status_code == 200
    assert "access" in response.data


def test_token_emitido_depois_da_troca_continua_valendo(api_client, manager_single_clinic):
    """Prova a folga do carimbo: `iat` é em segundos e o carimbo tem frações."""
    set_user_password(manager_single_clinic, NEW_PASSWORD)

    login(api_client, manager_single_clinic, password=NEW_PASSWORD)
    assert api_client.get(ME_URL).status_code == 200


# --------------------------------------------------------------------------- #
# RF-EQP-7: senha temporária tranca o resto do sistema até a troca
# --------------------------------------------------------------------------- #


@pytest.fixture
def attendant_with_temporary_password(db, attendant_a):
    set_user_password(attendant_a, PASSWORD, temporary=True)
    return attendant_a


def test_senha_temporaria_bloqueia_o_resto_do_sistema(
    api_client, attendant_with_temporary_password
):
    login(api_client, attendant_with_temporary_password)

    response = api_client.get(PATIENTS_URL)

    assert response.status_code == 403
    assert response.data["code"] == "password_change_required"


def test_senha_temporaria_deixa_ver_o_proprio_perfil(api_client, attendant_with_temporary_password):
    login(api_client, attendant_with_temporary_password)

    response = api_client.get(ME_URL)

    assert response.status_code == 200
    assert response.data["must_change_password"] is True


def test_troca_no_primeiro_acesso_dispensa_a_senha_atual(
    api_client, attendant_with_temporary_password
):
    login(api_client, attendant_with_temporary_password)

    response = api_client.post(PASSWORD_URL, {"new_password": NEW_PASSWORD}, format="json")

    assert response.status_code == 200
    attendant_with_temporary_password.refresh_from_db()
    assert attendant_with_temporary_password.must_change_password is False


def test_depois_da_troca_o_sistema_abre(api_client, attendant_with_temporary_password):
    login(api_client, attendant_with_temporary_password)
    novos = api_client.post(PASSWORD_URL, {"new_password": NEW_PASSWORD}, format="json").data

    api_client.credentials(HTTP_AUTHORIZATION=f"Bearer {novos['access']}")
    assert api_client.get(PATIENTS_URL).status_code == 200


def test_senha_nova_nao_pode_ser_a_temporaria(api_client, attendant_with_temporary_password):
    """Repetir a senha do papel deixaria a credencial ditada valendo para sempre."""
    login(api_client, attendant_with_temporary_password)

    response = api_client.post(PASSWORD_URL, {"new_password": PASSWORD}, format="json")

    assert response.status_code == 400
    assert "new_password" in response.data


# --------------------------------------------------------------------------- #
# RF-CTA-5: teto de tentativas
# --------------------------------------------------------------------------- #


def test_login_repetido_e_barrado(api_client, manager_single_clinic):
    for _ in range(10):
        api_client.post(
            TOKEN_URL,
            {"email": manager_single_clinic.email, "password": "errada"},
            format="json",
        )

    response = api_client.post(
        TOKEN_URL,
        {"email": manager_single_clinic.email, "password": PASSWORD},
        format="json",
    )
    assert response.status_code == 429


def test_teto_por_email_nao_tranca_outra_conta(api_client, manager_single_clinic, attendant_a):
    for _ in range(10):
        api_client.post(
            TOKEN_URL,
            {"email": manager_single_clinic.email, "password": "errada"},
            format="json",
        )

    # A colega do lado, no mesmo balcão, continua entrando.
    response = api_client.post(
        TOKEN_URL, {"email": attendant_a.email, "password": PASSWORD}, format="json"
    )
    assert response.status_code == 200


# --------------------------------------------------------------------------- #
# A senha temporária
# --------------------------------------------------------------------------- #


def test_senha_temporaria_e_ditavel_e_nunca_repete():
    senhas = {generate_temporary_password() for _ in range(50)}

    assert len(senhas) == 50, "senha temporária não pode repetir em uso normal"
    for senha in senhas:
        partes = senha.split("-")
        assert len(partes) == 4
        assert partes[-1].isdigit()
        assert all(parte.isalpha() and parte.isascii() for parte in partes[:-1])


def test_set_user_password_carimba_a_troca(db):
    user = User.objects.create_user(
        email="carimbo@teste.dev", password=PASSWORD, first_name="A", last_name="B"
    )
    antes = timezone.now() - timedelta(seconds=1)

    set_user_password(user, NEW_PASSWORD, temporary=True)

    user.refresh_from_db()
    assert user.password_changed_at > antes
    assert user.must_change_password is True
