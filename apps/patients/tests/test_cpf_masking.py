"""
CPF mascarado na saída da API (LGPD, §15).

Mascarar só no front não protegeria nada: o payload cru apareceria no devtools.
Estes testes olham a RESPOSTA da API, que é exatamente o que o navegador recebe.
"""

import pytest

from apps.core.masking import mask_cpf
from apps.patients.models import Patient

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


@pytest.fixture
def paciente(db, clinic_a):
    return Patient.objects.create(clinic=clinic_a, name="Paciente CPF", cpf=CPF_REAL)


def test_listagem_nao_devolve_o_cpf_inteiro(api_client, manager_single_clinic, paciente):
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.get(URL)

    (row,) = [p for p in response.data["results"] if p["id"] == paciente.pk]
    assert row["cpf"] == CPF_MASCARADO
    assert CPF_REAL not in str(response.data)


def test_ficha_nao_devolve_o_cpf_inteiro(api_client, manager_single_clinic, paciente):
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.get(f"{URL}{paciente.pk}/")

    assert response.data["cpf"] == CPF_MASCARADO
    assert CPF_REAL not in str(response.data)


def test_mascara_vale_para_todos_os_papeis(api_client, attendant_a, paciente):
    # Não é regra de papel: ninguém recebe o CPF inteiro pela API.
    api_client.force_authenticate(attendant_a)

    response = api_client.get(f"{URL}{paciente.pk}/")

    assert response.data["cpf"] == CPF_MASCARADO


def test_create_grava_o_cpf_inteiro_mas_responde_mascarado(
    api_client, manager_single_clinic, clinic_a
):
    """O EHR e a busca precisam do valor cheio — ele fica no banco, não na resposta."""
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.post(
        URL, {"name": "Nova Paciente", "cpf": CPF_REAL}, format="json"
    )

    assert response.status_code == 201
    assert response.data["cpf"] == CPF_MASCARADO
    assert Patient.objects.get(pk=response.data["id"]).cpf == CPF_REAL


def test_busca_por_cpf_continua_funcionando(
    api_client, manager_single_clinic, paciente
):
    """A busca roda no servidor, contra a coluna real — a máscara não a quebra."""
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.get(URL, {"search": "456"})

    assert [p["id"] for p in response.data["results"]] == [paciente.pk]
