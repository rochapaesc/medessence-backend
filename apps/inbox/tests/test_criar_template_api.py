"""
`POST /wa-templates/` — criar template e mandar para a revisão da Meta.

⚠️ O que estes testes protegem: o template criado aqui vai para a conta da
clínica na Meta e passa por revisão HUMANA. Uma chamada errada não é um 400
que se conserta em seguida: é um template recusado, com o nome já ocupado na
conta, e uma espera de horas para descobrir.
"""

import pytest

from apps.inbox.models import WhatsAppTemplate

URL = "/api/v1/wa-templates/"


def _corpo(**mudancas) -> dict:
    base = {
        "name": "retorno_paciente",
        "category": "MARKETING",
        "language": "pt_BR",
        "body": "Olá, {{1}}! Sentimos sua falta em {{2}}.",
        "examples": {"body": ["Ivanita", "Oeiras"]},
    }
    base.update(mudancas)
    return base


@pytest.mark.django_db
def test_criar_manda_para_a_meta_e_guarda_o_id(
    api_client, manager_single_clinic, inbox_a
):
    """
    ⚠️ O `meta_template_id` é o que permitirá editar e apagar ESTA variante de
    idioma sozinha: a Meta apaga pelo nome, e sem ele remove todas as línguas
    de uma vez.
    """
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(URL, _corpo(), format="json")

    assert resposta.status_code == 201
    template = WhatsAppTemplate.objects.get(name="retorno_paciente")
    assert template.meta_template_id
    # PENDING, e não APPROVED: a revisão é humana e leva de minutos a horas.
    assert template.status == "PENDING"
    assert template.clinic_id == inbox_a["conversation"].clinic_id


@pytest.mark.django_db
def test_o_que_foi_para_a_meta_e_o_payload_montado(
    api_client, manager_single_clinic, inbox_a, monkeypatch
):
    enviados = []

    class _Espiao:
        def create_template(self, payload):
            from apps.integrations.whatsapp.base import TemplateCriado

            enviados.append(payload)
            return TemplateCriado(id="tpl-1", status="PENDING")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Espiao(),
    )
    api_client.force_authenticate(manager_single_clinic)
    api_client.post(URL, _corpo(footer="MedEssence"), format="json")

    payload = enviados[0]
    assert [c["type"] for c in payload["components"]] == ["BODY", "FOOTER"]
    # Lista DE LISTAS no corpo: trocar pela lista simples é recusa na hora.
    corpo = payload["components"][0]
    assert corpo["example"] == {"body_text": [["Ivanita", "Oeiras"]]}


@pytest.mark.django_db
def test_erro_de_regra_da_meta_vem_ANTES_da_chamada(
    api_client, manager_single_clinic, inbox_a, monkeypatch
):
    """A alternativa é gastar a chamada e receber um 400 opaco horas depois."""
    chamou = []
    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: chamou.append(1),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(URL, _corpo(footer="Equipe {{1}}"), format="json")

    assert resposta.status_code == 400
    assert "rodapé não aceita variável" in str(resposta.data)
    assert chamou == [], "não pode ter falado com a Meta"


@pytest.mark.django_db
def test_template_recusado_pela_meta_NAO_some(
    api_client, manager_single_clinic, inbox_a, monkeypatch
):
    """
    ⚠️ Ele fica como rascunho local com o motivo (RF-INB-3.2.5). Sumir faria a
    clínica reescrever do zero um texto que ela acabou de digitar, sem saber o
    que estava errado.
    """
    from apps.integrations.whatsapp.exceptions import WhatsAppError

    class _Recusa:
        def create_template(self, payload):
            raise WhatsAppError("Conteúdo promocional não permitido nesta categoria.")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Recusa(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(URL, _corpo(), format="json")

    assert resposta.status_code == 201
    template = WhatsAppTemplate.objects.get(name="retorno_paciente")
    assert template.status == "REJECTED"
    assert "promocional" in template.rejection_reason
    assert template.components, "o texto digitado tem que continuar lá"


@pytest.mark.django_db
def test_nome_repetido_no_mesmo_idioma_e_recusado(
    api_client, manager_single_clinic, inbox_a, clinic_a
):
    WhatsAppTemplate.objects.create(
        clinic=clinic_a, name="retorno_paciente", language="pt_BR", status="APPROVED"
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.post(URL, _corpo(), format="json")

    assert resposta.status_code == 400
    assert "Já existe" in str(resposta.data)


@pytest.mark.django_db
def test_atendente_nao_cria_template(api_client, attendant_a, inbox_a):
    """
    O que se cria aqui vai para a conta da clínica na Meta e fica no nome
    dela. Ler é da recepção; criar é do gestor.
    """
    api_client.force_authenticate(attendant_a)
    resposta = api_client.post(URL, _corpo(), format="json")
    assert resposta.status_code == 403


@pytest.mark.django_db
def test_atendente_continua_LENDO_os_templates(api_client, attendant_a, inbox_a, clinic_a):
    WhatsAppTemplate.objects.create(
        clinic=clinic_a, name="confirmacao", language="pt_BR", status="APPROVED"
    )
    api_client.force_authenticate(attendant_a)
    resposta = api_client.get(URL)
    assert resposta.status_code == 200


@pytest.mark.django_db
def test_template_de_outra_clinica_nao_aparece(
    api_client, manager_single_clinic, inbox_a, clinic_b
):
    WhatsAppTemplate.objects.create(
        clinic=clinic_b, name="de_outra", language="pt_BR", status="APPROVED"
    )
    api_client.force_authenticate(manager_single_clinic)
    nomes = [t["name"] for t in api_client.get(URL).data["results"]]
    assert "de_outra" not in nomes


# --------------------------- editar e apagar --------------------------- #


@pytest.fixture
def na_meta(clinic_a):
    """Um template que JÁ existe na conta da Meta, como os criados por aqui."""
    return WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="retorno_paciente",
        language="pt_BR",
        category="MARKETING",
        status="APPROVED",
        meta_template_id="tpl-existente",
        components=[{"type": "BODY", "text": "Olá, {{1}}!"}],
    )


def _url(template) -> str:
    return f"{URL}{template.pk}/"


@pytest.mark.django_db
def test_editar_reescreve_na_meta_e_volta_para_revisao(
    api_client, manager_single_clinic, inbox_a, na_meta, monkeypatch
):
    """
    ⚠️ A Meta SUBSTITUI os componentes inteiros e devolve o template à fila de
    revisão. Deixar como APPROVED aqui faria a recepção mandar, achando que o
    texto novo já vale.
    """
    editados = []

    class _Espiao:
        def update_template(self, meta_template_id, payload):
            editados.append((meta_template_id, payload))

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Espiao(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        _url(na_meta),
        _corpo(body="Olá, {{1}}! Temos horário nesta semana.", examples={"body": ["Ivanita"]}),
        format="json",
    )

    assert resposta.status_code == 200
    na_meta.refresh_from_db()
    assert na_meta.status == "PENDING"
    assert "nesta semana" in na_meta.components[0]["text"]
    # O id da variante vai junto: é por ele que a Meta acha o template.
    assert editados[0][0] == "tpl-existente"


@pytest.mark.django_db
def test_template_em_revisao_nao_se_edita(
    api_client, manager_single_clinic, inbox_a, na_meta
):
    na_meta.status = "PENDING"
    na_meta.save(update_fields=["status"])
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(_url(na_meta), _corpo(examples={"body": ["a", "b"]}), format="json")

    assert resposta.status_code == 400
    assert "em revisão" in str(resposta.data)


@pytest.mark.django_db
def test_template_que_nunca_foi_para_a_meta_se_CRIA_e_nao_se_edita(
    api_client, manager_single_clinic, inbox_a, clinic_a
):
    local = WhatsAppTemplate.objects.create(
        clinic=clinic_a, name="rascunho", language="pt_BR", status="REJECTED"
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        _url(local), _corpo(name="rascunho", examples={"body": ["a", "b"]}), format="json"
    )

    assert resposta.status_code == 400
    assert "Crie um novo" in str(resposta.data)


@pytest.mark.django_db
def test_nome_e_idioma_nao_mudam(api_client, manager_single_clinic, inbox_a, na_meta):
    """A Meta não deixa renomear nem trocar o idioma de um template existente."""
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        _url(na_meta), _corpo(name="outro_nome", examples={"body": ["a", "b"]}), format="json"
    )

    assert resposta.status_code == 400
    assert "nome de um template não pode mudar" in str(resposta.data)


@pytest.mark.django_db
def test_meta_recusando_a_edicao_MANTEM_o_que_esta_no_ar(
    api_client, manager_single_clinic, inbox_a, na_meta, monkeypatch
):
    """
    ⚠️ Guardar a versão nova aqui faria a tela mostrar um texto que o paciente
    não vai receber: na Meta continua valendo o de antes.
    """
    from apps.integrations.whatsapp.exceptions import WhatsAppError

    class _Recusa:
        def update_template(self, meta_template_id, payload):
            raise WhatsAppError("Template em uso por uma campanha ativa.")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Recusa(),
    )
    antes = na_meta.components
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        _url(na_meta), _corpo(body="Texto novo {{1}}", examples={"body": ["a"]}), format="json"
    )

    assert resposta.status_code == 400
    na_meta.refresh_from_db()
    assert na_meta.components == antes
    assert na_meta.status == "APPROVED"
    assert "campanha ativa" in na_meta.rejection_reason


@pytest.mark.django_db
def test_apagar_manda_o_ID_junto_para_nao_levar_os_outros_idiomas(
    api_client, manager_single_clinic, inbox_a, na_meta, monkeypatch
):
    """
    ⚠️ Sem o `meta_template_id`, a Meta apaga TODAS as variantes de idioma com
    aquele nome — inclusive as que ninguém pediu para apagar.
    """
    apagados = []

    class _Espiao:
        def delete_template(self, name, meta_template_id=""):
            apagados.append((name, meta_template_id))

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Espiao(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.delete(_url(na_meta))

    assert resposta.status_code == 204
    assert apagados == [("retorno_paciente", "tpl-existente")]
    assert not WhatsAppTemplate.objects.filter(pk=na_meta.pk).exists()


@pytest.mark.django_db
def test_meta_recusando_a_exclusao_NAO_apaga_aqui(
    api_client, manager_single_clinic, inbox_a, na_meta, monkeypatch
):
    """
    ⚠️ Apagar o nosso e falhar lá deixaria um template órfão na conta da
    clínica: nome ocupado, e nada por aqui apontando para ele.
    """
    from apps.integrations.whatsapp.exceptions import WhatsAppError

    class _Recusa:
        def delete_template(self, name, meta_template_id=""):
            raise WhatsAppError("Template em uso.")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: _Recusa(),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.delete(_url(na_meta))

    assert resposta.status_code == 400
    assert WhatsAppTemplate.objects.filter(pk=na_meta.pk).exists()


@pytest.mark.django_db
def test_apagar_o_que_so_existe_aqui_nao_chama_a_meta(
    api_client, manager_single_clinic, inbox_a, clinic_a, monkeypatch
):
    local = WhatsAppTemplate.objects.create(
        clinic=clinic_a, name="rascunho", language="pt_BR", status="REJECTED"
    )
    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda c: pytest.fail("não devia falar com a Meta"),
    )
    api_client.force_authenticate(manager_single_clinic)

    assert api_client.delete(_url(local)).status_code == 204


@pytest.mark.django_db
def test_atendente_nao_edita_nem_apaga(api_client, attendant_a, inbox_a, na_meta):
    api_client.force_authenticate(attendant_a)
    assert api_client.delete(_url(na_meta)).status_code == 403
    assert (
        api_client.put(_url(na_meta), _corpo(examples={"body": ["a", "b"]}), format="json").status_code
        == 403
    )
