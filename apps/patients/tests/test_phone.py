"""Regra do nono dígito (§6.2): canônico, grafias, busca e a constraint viva."""

from apps.patients.models import Contact, Patient
from apps.patients.phone import (
    canonizar_telefone,
    grafia_alternativa,
    grafias_de_busca,
    pode_ser_celular,
)

URL = "/api/v1/patients/"


# ------------------------- funções puras -------------------------


def test_canonico_poe_ddi_e_nono_digito():
    assert canonizar_telefone("(85) 98876-5432") == "5585988765432"
    assert canonizar_telefone("85 8876-5432") == "5585988765432"  # celular antigo ganha o 9
    assert canonizar_telefone("558588765432") == "5585988765432"  # com DDI, sem o 9
    assert canonizar_telefone("5585988765432") == "5585988765432"  # já canônico: intocado


def test_fixo_nunca_ganha_nove():
    # O bug do Chatwoot que não herdamos: assinante começando em 2-5 é fixo.
    assert canonizar_telefone("(85) 3244-1100") == "558532441100"
    assert pode_ser_celular("(85) 3244-1100") is False


def test_vazio_e_nao_br_ficam_como_estao():
    assert canonizar_telefone("") == ""
    assert canonizar_telefone(None) == ""
    assert canonizar_telefone("+351 912 345 678") == "351912345678"
    assert pode_ser_celular("+351 912 345 678") is True  # fora do BR não dá para afirmar fixo
    assert pode_ser_celular("") is False
    assert pode_ser_celular("(85) 98876-5432") is True


def test_grafia_alternativa_so_existe_para_celular_br():
    assert grafia_alternativa("5585988765432") == "558588765432"
    assert grafia_alternativa("558588765432") == "5585988765432"
    assert grafia_alternativa("558532441100") is None  # fixo
    assert grafia_alternativa("351912345678") is None  # não-BR
    assert grafia_alternativa("") is None


def test_grafias_de_busca_cobrem_as_quatro_formas():
    grafias = grafias_de_busca("5585988765432")
    assert "5585988765432" in grafias  # canônica (EHR)
    assert "558588765432" in grafias  # wa_id sem o 9 (Meta antiga)
    assert "85988765432" in grafias  # form local com 9
    assert "8588765432" in grafias  # form local sem 9
    assert grafias_de_busca("") == []


# ------------------------- API: save e busca -------------------------


def test_create_normaliza_telefone_para_o_canonico(api_client, manager_single_clinic, clinic_a):
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        URL, {"name": "Marta Oliveira", "phone": "(85) 98876-5432"}, format="json"
    )
    assert response.status_code == 201
    salvo = Patient.objects.get(clinic=clinic_a, name="Marta Oliveira")
    assert salvo.phone == "5585988765432"


def test_busca_acha_o_numero_em_qualquer_grafia(api_client, manager_single_clinic, clinic_a):
    # As três portas históricas do telefone: EHR (55+13), form antigo (local),
    # Meta sem o 9 — e um quarto número que NÃO pode vir (teste de exclusão).
    Patient.objects.create(clinic=clinic_a, name="Do EHR", phone="5585988765432")
    Patient.objects.create(clinic=clinic_a, name="Do Form", phone="85988765432")
    Patient.objects.create(clinic=clinic_a, name="Sem Nove", phone="558588765432")
    Patient.objects.create(clinic=clinic_a, name="Outro Numero", phone="5585911112222")
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.get(URL, {"search": "5585988765432"})  # como o seletor manda (wa_id)
    names = {item["name"] for item in response.data["results"]}
    assert names == {"Do EHR", "Do Form", "Sem Nove"}


def test_busca_sem_correspondencia_devolve_vazio(api_client, manager_single_clinic, clinic_a):
    Patient.objects.create(clinic=clinic_a, name="Marta", phone="5585988765432")
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get(URL, {"search": "5585900000000"})
    assert response.data["results"] == []


# ------------------------- constraint entre vivos -------------------------


def test_recriar_contato_soft_deletado_nao_estoura(clinic_a):
    contato = Contact.objects.create(clinic=clinic_a, wa_id="5585999990009")
    contato.delete()  # soft delete do projeto
    de_novo = Contact.objects.create(clinic=clinic_a, wa_id="5585999990009")
    assert de_novo.pk != contato.pk
