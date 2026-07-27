"""
CPF por papel na saída da API (§15, decidido em 21/07/2026).

Médico e gestor precisam do documento para atender e faturar; o atendente vê
mascarado — mesma régua do conteúdo clínico (P10). A decisão é do SERVIDOR:
mascarar só no front não protegeria nada, o payload cru apareceria no devtools.
Estes testes olham a RESPOSTA da API, que é o que o navegador recebe.
"""

import pytest

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership
from apps.core.masking import is_masked, mask_cpf
from apps.patients.models import Patient
from conftest import make_user

URL = "/api/v1/patients/"
CPF_REAL = "123.456.789-00"
CPF_MASCARADO = "123.***.***-00"


def test_mask_cpf_revela_tres_primeiros_e_dois_ultimos():
    assert mask_cpf(CPF_REAL) == CPF_MASCARADO
    assert mask_cpf("12345678900") == "123******00"


def test_mask_cpf_preserva_vazio_e_esconde_valor_curto_demais():
    assert mask_cpf("") == ""
    assert mask_cpf(None) == ""
    # Com poucos dígitos, revelar as pontas entregaria quase tudo.
    assert mask_cpf("1234") == "****"


def test_is_masked_reconhece_o_que_a_tela_devolve():
    assert is_masked(CPF_MASCARADO)
    assert is_masked("123******00")
    assert not is_masked(CPF_REAL)
    assert not is_masked("")
    assert not is_masked(None)


@pytest.fixture
def paciente(db, clinic_a):
    return Patient.objects.create(clinic=clinic_a, name="Paciente CPF", cpf=CPF_REAL)


@pytest.fixture
def doctor_a(db, clinic_a):
    user = make_user("medico.cpf@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.DOCTOR)
    return user


# ───────────────────────────── leitura ──────────────────────────────────


def test_gestor_ve_o_cpf_inteiro_na_ficha(api_client, manager_single_clinic, paciente):
    api_client.force_authenticate(manager_single_clinic)

    ficha = api_client.get(f"{URL}{paciente.pk}/")

    assert ficha.data["cpf"] == CPF_REAL


def test_listagem_mascara_para_todos(api_client, manager_single_clinic, paciente):
    """A lista não mostra documento na tela — devolvê-lo seria expor em massa
    (e o acesso um a um é o que a auditoria consegue registrar de forma útil)."""
    api_client.force_authenticate(manager_single_clinic)

    listagem = api_client.get(URL)

    (row,) = [p for p in listagem.data["results"] if p["id"] == paciente.pk]
    assert row["cpf"] == CPF_MASCARADO
    assert CPF_REAL not in str(listagem.data)


def test_medico_ve_o_cpf_inteiro(api_client, doctor_a, paciente):
    api_client.force_authenticate(doctor_a)

    response = api_client.get(f"{URL}{paciente.pk}/")

    assert response.data["cpf"] == CPF_REAL


def test_atendente_ve_mascarado(api_client, attendant_a, paciente):
    api_client.force_authenticate(attendant_a)

    ficha = api_client.get(f"{URL}{paciente.pk}/")
    assert ficha.data["cpf"] == CPF_MASCARADO
    assert CPF_REAL not in str(ficha.data)

    listagem = api_client.get(URL)
    assert CPF_REAL not in str(listagem.data)


# ───────────────────────────── escrita ──────────────────────────────────


def test_create_grava_e_devolve_o_cpf_inteiro_para_quem_ve(
    api_client, manager_single_clinic, clinic_a
):
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.post(
        URL, {"name": "Nova Paciente", "cpf": CPF_REAL}, format="json"
    )

    assert response.status_code == 201
    assert response.data["cpf"] == CPF_REAL
    assert Patient.objects.get(pk=response.data["id"]).cpf == CPF_REAL


def test_atendente_cadastra_mas_recebe_mascarado(api_client, attendant_a, clinic_a):
    """O atendente CADASTRA (é o trabalho dele) sem passar a ver o documento."""
    api_client.force_authenticate(attendant_a)

    response = api_client.post(
        URL, {"name": "Paciente do Atendente", "cpf": CPF_REAL}, format="json"
    )

    assert response.status_code == 201
    assert response.data["cpf"] == CPF_MASCARADO
    # Gravou o valor real: o EHR e a busca precisam dele.
    assert Patient.objects.get(pk=response.data["id"]).cpf == CPF_REAL


def test_editar_com_cpf_mascarado_nao_apaga_o_documento(
    api_client, attendant_a, paciente
):
    """
    REGRESSÃO (21/07/2026): a tela mostrava `123.***.***-00`, o formulário
    reenviava isso no salvar e o CPF real virava `12300` — e seguia truncado
    para o EHR. Máscara chegando na escrita significa "não mudou".
    """
    api_client.force_authenticate(attendant_a)

    response = api_client.patch(
        f"{URL}{paciente.pk}/",
        {"name": "Nome Editado", "cpf": CPF_MASCARADO},
        format="json",
    )

    assert response.status_code == 200
    paciente.refresh_from_db()
    assert paciente.cpf == CPF_REAL, "o documento real tem de sobreviver à edição"
    assert paciente.name == "Nome Editado"


def test_cadastro_novo_com_cpf_mascarado_e_recusado(
    api_client, manager_single_clinic, clinic_a
):
    """Sem registro anterior não há o que preservar - é dado inválido."""
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.post(
        URL, {"name": "Paciente Suspeito", "cpf": CPF_MASCARADO}, format="json"
    )

    assert response.status_code == 400
    assert "cpf" in response.data


# ─────────────────────── gate clínico na escrita ────────────────────────


def test_atendente_nao_le_conteudo_clinico_pela_resposta_do_update(
    api_client, attendant_a, paciente
):
    """
    REGRESSÃO (21/07/2026): o gate P10 valia na leitura, mas a resposta do
    create/update devolvia `comments_html` inteiro — bastava salvar qualquer
    campo para contornar.
    """
    paciente.comments_html = "<p>Observação clínica sensível</p>"
    paciente.save(update_fields=["comments_html"])

    api_client.force_authenticate(attendant_a)
    response = api_client.patch(
        f"{URL}{paciente.pk}/", {"name": "Outro Nome"}, format="json"
    )

    assert response.status_code == 200
    assert "comments_html" not in response.data
    assert "sensível" not in str(response.data)


def test_medico_continua_lendo_o_conteudo_clinico_no_update(
    api_client, doctor_a, paciente
):
    paciente.comments_html = "<p>Observação clínica</p>"
    paciente.save(update_fields=["comments_html"])

    api_client.force_authenticate(doctor_a)
    response = api_client.patch(
        f"{URL}{paciente.pk}/", {"name": "Nome Novo"}, format="json"
    )

    assert response.data["comments_html"] == "<p>Observação clínica</p>"


def test_busca_por_cpf_continua_funcionando(
    api_client, manager_single_clinic, paciente
):
    """A busca roda no servidor, contra a coluna real — a máscara não a quebra."""
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.get(URL, {"search": "456"})

    assert [p["id"] for p in response.data["results"]] == [paciente.pk]
