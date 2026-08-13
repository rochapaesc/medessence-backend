"""
A mensagem de resgate (RF-REA-2.2/2.3/2.4): qual template sai, o que cada
variável recebe e como a mensagem chega.

A prévia é o ponto: ela existe para mostrar o que quebra ANTES de a mensagem
ir para milhares de pessoas, e o que quebra na clínica real é o prontuário
guardar tudo em caixa alta.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.inbox.models import ReactivationMessage, WhatsAppTemplate
from apps.inbox.reactivation import nome_proprio, variaveis_do_template
from apps.patients.models import Patient

URL = "/api/v1/reactivation-message/"


@pytest.fixture
def template(clinic_a):
    return WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="retorno_paciente",
        language="pt_BR",
        category="MARKETING",
        status="APPROVED",
        components=[
            {
                "type": "BODY",
                "text": "Olá, {{1}}! Sentimos sua falta em {{2}}. {{3}}",
            }
        ],
    )


@pytest.fixture
def paciente_caixa_alta(clinic_a):
    """Como o prontuário guarda de verdade: tudo em caixa alta."""
    return Patient.objects.create(
        clinic=clinic_a,
        name="IVANITA DIAS DE SOUSA",
        city="SÃO JOÃO DO PIAUÍ",
        last_appointment_at=timezone.now() - timedelta(days=400),
    )


def mapa_valido():
    return {
        "1": {"source": "patient_first_name"},
        "2": {"source": "patient_city"},
        "3": {"source": "fixed", "value": "Temos horário esta semana."},
    }


@pytest.mark.parametrize(
    ("cru", "esperado"),
    [
        ("IVANITA DIAS DE SOUSA", "Ivanita Dias de Sousa"),
        ("SÃO JOÃO DO PIAUÍ", "São João do Piauí"),
        ("DOM INOCÊNCIO", "Dom Inocêncio"),
        # Caixa mista é escolha de quem digitou, e não se mexe nela.
        ("McArthur Silva", "McArthur Silva"),
        ("", ""),
        (None, ""),
    ],
)
def test_caixa_de_titulo_so_mexe_no_que_veio_gritando(cru, esperado):
    assert nome_proprio(cru) == esperado


def test_variaveis_sem_repetir_e_em_ordem_numerica(clinic_a):
    """
    A Meta não garante ordem no texto, e a mesma variável pode aparecer duas
    vezes na frase.
    """
    template = WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="bagunçado",
        status="APPROVED",
        components=[{"type": "BODY", "text": "{{10}} {{2}} de novo {{2}} e {{1}}"}],
    )
    assert variaveis_do_template(template) == ["1", "2", "10"]


def test_previa_monta_com_paciente_real_da_fila(
    api_client, manager_single_clinic, clinic_a, template, paciente_caixa_alta
):
    """RF-REA-2.4: a prévia usa gente de verdade, não `[Nome]`."""
    api_client.force_authenticate(manager_single_clinic)

    salvo = api_client.put(
        URL, {"template": template.pk, "variables": mapa_valido()}, format="json"
    )
    assert salvo.status_code == 200
    assert salvo.data["preview_patient"] == "IVANITA DIAS DE SOUSA"
    assert salvo.data["preview"] == (
        "Olá, Ivanita! Sentimos sua falta em São João do Piauí. "
        "Temos horário esta semana."
    )


def test_previa_vazia_sem_template_escolhido(
    api_client, manager_single_clinic, template, paciente_caixa_alta
):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(URL)
    assert resposta.data["template"] is None
    assert resposta.data["preview"] == ""
    assert [t["name"] for t in resposta.data["available_templates"]] == [
        "retorno_paciente"
    ]


def test_valores_do_exemplo_vao_resolvidos_para_a_tela(
    api_client, manager_single_clinic, clinic_a, template, paciente_caixa_alta
):
    """
    O drawer monta a prévia do RASCUNHO com estes valores, sem uma ida ao
    servidor por troca de fonte. Eles vão resolvidos daqui para os dois lados
    produzirem a MESMA frase: se o front capitalizasse por conta própria, a
    prévia do rascunho sairia diferente da mensagem enviada depois de salvar.
    """
    api_client.force_authenticate(manager_single_clinic)
    fontes = api_client.get(URL).data["preview_sources"]

    assert fontes["patient_first_name"] == "Ivanita"
    assert fontes["patient_full_name"] == "Ivanita Dias de Sousa"
    assert fontes["patient_city"] == "São João do Piauí"
    assert fontes["clinic_name"] == clinic_a.name
    # Texto fixo não sai daqui: ele é digitado, não vem do cadastro.
    assert "fixed" not in fontes


def test_sem_ninguem_na_fila_nao_ha_exemplo(
    api_client, manager_single_clinic, template
):
    """Clínica nova não tem fila, e a tela não pode estourar por isso."""
    api_client.force_authenticate(manager_single_clinic)
    dados = api_client.get(URL).data
    assert dados["preview_patient"] == ""
    assert dados["preview_sources"] == {}
    assert dados["preview"] == ""


def test_template_nao_aprovado_fica_fora_da_lista(
    api_client, manager_single_clinic, clinic_a, template
):
    WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="ainda_em_analise",
        status="PENDING",
        components=[{"type": "BODY", "text": "oi"}],
    )
    api_client.force_authenticate(manager_single_clinic)
    nomes = [t["name"] for t in api_client.get(URL).data["available_templates"]]
    assert nomes == ["retorno_paciente"]
    assert "ainda_em_analise" not in nomes


def test_mapa_incompleto_e_recusado(
    api_client, manager_single_clinic, template
):
    """
    Adiar isso para a hora do disparo é adiar para quando ninguém está olhando.
    """
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        URL,
        {"template": template.pk, "variables": {"1": {"source": "patient_first_name"}}},
        format="json",
    )
    assert resposta.status_code == 400
    assert "2" in str(resposta.data["variables"])


def test_variavel_que_o_template_nao_usa_e_recusada(
    api_client, manager_single_clinic, template
):
    api_client.force_authenticate(manager_single_clinic)
    mapa = mapa_valido() | {"9": {"source": "clinic_name"}}
    resposta = api_client.put(
        URL, {"template": template.pk, "variables": mapa}, format="json"
    )
    assert resposta.status_code == 400
    assert "9" in str(resposta.data["variables"])


def test_texto_fixo_em_branco_e_recusado(
    api_client, manager_single_clinic, template
):
    """Sairia como buraco no meio da frase, e ninguém perceberia."""
    api_client.force_authenticate(manager_single_clinic)
    mapa = mapa_valido() | {"3": {"source": "fixed", "value": "   "}}
    resposta = api_client.put(
        URL, {"template": template.pk, "variables": mapa}, format="json"
    )
    assert resposta.status_code == 400


def test_template_de_outra_clinica_e_recusado(
    api_client, manager_single_clinic, clinic_b
):
    alheio = WhatsAppTemplate.objects.create(
        clinic=clinic_b,
        name="de_outra_clinica",
        status="APPROVED",
        components=[{"type": "BODY", "text": "oi"}],
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        URL, {"template": alheio.pk, "variables": {}}, format="json"
    )
    assert resposta.status_code == 400


@pytest.mark.parametrize("status", ["PENDING", "REJECTED", "PAUSED", "DISABLED"])
def test_template_que_a_meta_nao_aprovou_e_recusado(
    api_client, manager_single_clinic, clinic_a, status
):
    """
    ⚠️ A configuração daqui é PERSISTENTE e sai depois para a fila inteira.
    Guardar um template que a Meta não aceita adiaria a recusa para a hora do
    disparo, quando ninguém está olhando a tela - e a tela nem oferece esses,
    então quem chegar aqui veio por fora dela.
    """
    reprovado = WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="ainda_nao_vale",
        language="pt_BR",
        category="MARKETING",
        status=status,
        components=[{"type": "BODY", "text": "Olá!"}],
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.put(
        URL, {"template": reprovado.pk, "variables": {}}, format="json"
    )

    assert resposta.status_code == 400
    assert "aprovado" in str(resposta.data)


def test_limpar_o_template_limpa_o_mapa(
    api_client, manager_single_clinic, clinic_a, template, paciente_caixa_alta
):
    """Mapa órfão apontaria variáveis de um template que não sai mais."""
    api_client.force_authenticate(manager_single_clinic)
    api_client.put(
        URL, {"template": template.pk, "variables": mapa_valido()}, format="json"
    )

    resposta = api_client.put(URL, {"template": None}, format="json")
    assert resposta.status_code == 200
    assert resposta.data["template"] is None
    assert resposta.data["variables"] == {}
    assert ReactivationMessage.objects.get(clinic=clinic_a).variables == {}


def test_atendente_nao_escolhe_a_mensagem(api_client, attendant_a, template):
    """
    Mesma régua do horário de funcionamento: quem escolhe o template decide o
    que a fila inteira recebe.
    """
    api_client.force_authenticate(attendant_a)
    assert api_client.get(URL).status_code == 403
    assert (
        api_client.put(
            URL, {"template": template.pk, "variables": mapa_valido()}, format="json"
        ).status_code
        == 403
    )


def test_a_campanha_recebe_os_MESMOS_campos_do_inbox(
    api_client, manager_single_clinic, clinic_a
):
    """
    ⚠️ A campanha é o TERCEIRO lugar que manda template, e ficou de fora
    quando o Inbox e o nó de fluxo ganharam os rótulos (12/08/2026). Sem eles
    a tela desenha só os `{{n}}` do corpo: o link do botão fica sem campo, a
    pessoa salva achando que preencheu tudo, e a Meta recusa o disparo INTEIRO
    - para a base toda de uma vez, e não para uma conversa só.
    """
    WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="com_botao",
        language="pt_BR",
        category="MARKETING",
        status="APPROVED",
        components=[
            {"type": "BODY", "text": "Olá, {{1}}!"},
            {
                "type": "BUTTONS",
                "buttons": [
                    {
                        "type": "URL",
                        "text": "Agendar",
                        "url": "https://clinica.com.br/agenda/{{1}}",
                    }
                ],
            },
        ],
    )
    api_client.force_authenticate(manager_single_clinic)
    achado = next(
        t
        for t in api_client.get(URL).data["available_templates"]
        if t["name"] == "com_botao"
    )

    assert achado["variables"] == ["1", "button:0:1"]
    assert achado["variable_labels"]["button:0:1"] == 'final do link do botão "Agendar"'
    # O modelo do link vai junto para a tela mostrar o endereço se formando:
    # o campo quer o FINAL da URL, não a URL inteira.
    assert achado["variable_url_templates"] == {
        "button:0:1": "https://clinica.com.br/agenda/{{1}}"
    }


def test_template_sem_botao_nao_leva_modelo_de_link(
    api_client, manager_single_clinic, template
):
    api_client.force_authenticate(manager_single_clinic)
    achado = api_client.get(URL).data["available_templates"][0]
    assert achado["variable_url_templates"] == {}
    assert achado["variable_labels"] == {"1": "{{1}}", "2": "{{2}}", "3": "{{3}}"}


def test_opcao_leva_os_componentes_crus(
    api_client, manager_single_clinic, clinic_a
):
    """
    A tela desenha o template como a mensagem que ele é, e o cabeçalho, o
    rodapé e os botões só existem nos componentes: sem eles a campanha lista
    o corpo em cinza e esconde metade do que a fila vai receber.
    """
    WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="com_rodape",
        language="pt_BR",
        category="MARKETING",
        status="APPROVED",
        components=[
            {"type": "HEADER", "format": "TEXT", "text": "Sentimos sua falta"},
            {"type": "BODY", "text": "Olá, {{1}}!"},
            {"type": "FOOTER", "text": "Responda SAIR para não receber mais"},
        ],
    )
    api_client.force_authenticate(manager_single_clinic)
    achado = next(
        t
        for t in api_client.get(URL).data["available_templates"]
        if t["name"] == "com_rodape"
    )

    assert [c["type"] for c in achado["components"]] == ["HEADER", "BODY", "FOOTER"]
