"""
O nó "Enviar template" (RF-FLW-24).

⚠️ O defeito que estes testes trancam: o nó mandava TEXTO LIVRE. Ele só
preenchia `body` e nunca `template_name`, e o envio - que decide pelo nome -
caía no `send_text`. Como o nó existe justamente para falar FORA da janela de
24h, ele falhava no único caso em que serve, e a Meta recusava.

Passou despercebido por dois motivos que valem mais que o bug: o nó nunca foi
usado num fluxo de verdade, e o VALIDADOR cobrava `template_name` enquanto o
MOTOR lia `text` - cada lado testava o seu, e ninguém cruzou os dois.
"""

import pytest

from apps.automation.choices import EDGE_DEFAULT, FlowNodeType, FlowStatus
from apps.automation.graph import validate_graph
from apps.inbox.choices import MessageKind, VariableSource
from apps.inbox.models import WhatsAppTemplate
from apps.inbox.template_vars import (
    Contexto,
    componentes_para_a_meta,
    modelo_do_link,
    parametros,
    rotulo_da_variavel,
    variaveis_do_template,
)


@pytest.fixture
def template(clinic_a):
    """Como os da clínica real: com variáveis no corpo."""
    return WhatsAppTemplate.objects.create(
        clinic=clinic_a,
        name="retorno_paciente",
        language="pt_BR",
        category="MARKETING",
        status="APPROVED",
        components=[
            {"type": "BODY", "text": "Olá, {{1}}! Sentimos sua falta em {{2}}."}
        ],
    )


def _grafo(config: dict) -> dict:
    return {
        "entry_node": "n1",
        "nodes": [
            {"id": "n1", "type": FlowNodeType.START, "config": {}},
            {
                "id": "n2",
                "type": FlowNodeType.SEND_TEMPLATE,
                "label": "Convite de retorno",
                "config": config,
            },
            {"id": "n3", "type": FlowNodeType.END, "config": {}},
        ],
        "edges": [
            {"from": "n1", "to": "n2", "condition": EDGE_DEFAULT},
            {"from": "n2", "to": "n3", "condition": EDGE_DEFAULT},
        ],
    }


class _FakeTemplate:
    def __init__(self, texto, *, header=None, buttons=None, midia=None):
        self.components = []
        if header is not None:
            self.components.append({"type": "HEADER", "format": "TEXT", "text": header})
        if midia is not None:
            # Como a Meta devolve: o `header_handle` é o exemplo da CRIAÇÃO.
            self.components.append(
                {
                    "type": "HEADER",
                    "format": midia,
                    "example": {"header_handle": ["https://scontent.meta/exemplo"]},
                }
            )
        self.components.append({"type": "BODY", "text": texto})
        if buttons is not None:
            self.components.append({"type": "BUTTONS", "buttons": buttons})


class TestOsParametros:
    """
    A lista que vai para a Meta, na ordem em que ela casa: por POSIÇÃO.

    O mapa é o MESMO formato dos três lugares (`{"1": {"source": ...}}`), e não
    mais um texto por variável: resolver de dois jeitos é como a mensagem
    começa a sair diferente da prévia.
    """

    def test_a_fonte_flow_var_busca_no_que_o_fluxo_coletou(self):
        template = _FakeTemplate("Olá, {{1}}! Você é de {{2}}.")
        mapa = {
            "1": {"source": VariableSource.FLOW_VAR, "value": "nome"},
            "2": {"source": VariableSource.FLOW_VAR, "value": "cidade"},
        }
        contexto = Contexto(flow_vars={"nome": "Ivanita", "cidade": "Oeiras"})
        assert parametros(template, mapa, contexto) == {
            "1": "Ivanita",
            "2": "Oeiras",
        }

    def test_buraco_na_numeracao_nao_desloca_o_resto(self):
        """
        Sem a posição vazia, `{{3}}` receberia o valor destinado ao `{{2}}` e
        a mensagem sairia com o dado trocado, sem erro nenhum.
        """
        template = _FakeTemplate("{{1}} {{3}}")
        mapa = {
            "1": {"source": VariableSource.FIXED, "value": "a"},
            "3": {"source": VariableSource.FIXED, "value": "c"},
        }
        # O buraco aparece na LISTA que vai para a Meta, que casa por posição.
        components = componentes_para_a_meta(
            template, parametros(template, mapa, Contexto())
        )
        assert [p["text"] for p in components[0]["parameters"]] == ["a", "", "c"]

    def test_variavel_de_fluxo_que_nao_existe_vira_vazio(self):
        """Mesma regra do resto do motor: o paciente não lê `{{chave}}`."""
        template = _FakeTemplate("Olá, {{1}}!")
        mapa = {"1": {"source": VariableSource.FLOW_VAR, "value": "inexistente"}}
        assert parametros(template, mapa, Contexto(flow_vars={})) == {"1": ""}

    def test_sem_mapa_nao_manda_parametro(self):
        assert parametros(_FakeTemplate("sem variável"), {}, Contexto()) == {}


class TestOsComponentesDaMeta:
    """
    O que o `send_template` recebe.

    ⚠️ Estes casos vêm do `template-send-builder` do wacrm (MIT), que já tinha
    o problema resolvido. Foi o que o teste ao vivo de 12/08/2026 mostrou: o
    corpo ia certo e a Meta recusava a mensagem INTEIRA com
    `(#131008) Button at index 0 of type Url requires a parameter`, porque o
    botão de URL dinâmica ficava sem parâmetro.
    """

    def test_o_botao_de_url_vira_component_PROPRIO(self):
        template = _FakeTemplate(
            "Olá, {{1}}!",
            buttons=[{"type": "URL", "text": "Acessar", "url": "https://x/{{1}}"}],
        )
        components = componentes_para_a_meta(
            template, {"1": "Ivanita", "button:0:1": "agenda.pdf"}
        )
        assert components == [
            {"type": "body", "parameters": [{"type": "text", "text": "Ivanita"}]},
            {
                "type": "button",
                "sub_type": "url",
                "index": "0",
                "parameters": [{"type": "text", "text": "agenda.pdf"}],
            },
        ]

    def test_botao_de_url_SEM_variavel_nao_entra(self):
        """O template carrega a URL inteira: mandar parâmetro seria erro."""
        template = _FakeTemplate(
            "Olá, {{1}}!",
            buttons=[{"type": "URL", "text": "Site", "url": "https://x/fixo"}],
        )
        assert variaveis_do_template(template) == ["1"]

    def test_quick_reply_e_telefone_nunca_pedem_parametro(self):
        template = _FakeTemplate(
            "Oi",
            buttons=[
                {"type": "QUICK_REPLY", "text": "Sim"},
                {"type": "PHONE_NUMBER", "text": "Ligar", "phone_number": "+55"},
            ],
        )
        assert variaveis_do_template(template) == []

    def test_o_indice_do_botao_conta_os_que_vem_antes(self):
        """
        ⚠️ A Meta casa o component pelo índice no array INTEIRO: um
        quick-reply antes do URL desloca o índice, e mandar `0` faria o
        parâmetro cair no botão errado.
        """
        template = _FakeTemplate(
            "Oi",
            buttons=[
                {"type": "QUICK_REPLY", "text": "Sim"},
                {"type": "URL", "text": "Ver", "url": "https://x/{{1}}"},
            ],
        )
        assert variaveis_do_template(template) == ["button:1:1"]

    def test_o_cabecalho_com_variavel_vira_component(self):
        template = _FakeTemplate("Corpo", header="Aviso de {{1}}")
        components = componentes_para_a_meta(template, {"header:1": "hoje"})
        assert components[0] == {
            "type": "header",
            "parameters": [{"type": "text", "text": "hoje"}],
        }

    def test_cabecalho_ESTATICO_nao_entra(self):
        """O template já carrega o texto; mandar component seria erro."""
        template = _FakeTemplate("Olá, {{1}}!", header="Comunicado Escolar")
        assert variaveis_do_template(template) == ["1"]

    def test_a_ordem_e_cabecalho_corpo_botoes(self):
        template = _FakeTemplate(
            "Olá, {{1}}!",
            header="Aviso de {{1}}",
            buttons=[{"type": "URL", "text": "Ver", "url": "https://x/{{1}}"}],
        )
        components = componentes_para_a_meta(
            template,
            {"header:1": "hoje", "1": "Ivanita", "button:0:1": "a.pdf"},
        )
        assert [c["type"] for c in components] == ["header", "body", "button"]

    def test_sem_parametros_devolve_None_e_nao_lista_vazia(self):
        """A Meta recusa `components: []` tanto quanto parâmetro faltando."""
        assert componentes_para_a_meta(_FakeTemplate("sem variável"), {}) is None


class TestOCabecalhoDeMidia:
    """
    ⚠️ A Meta exige o component de mídia em TODO envio, mesmo sem variável e
    mesmo com a imagem inalterada desde a aprovação (wacrm: "Meta requires the
    media component on every send"). Sem ele a mensagem INTEIRA é recusada, e
    o erro não diz que o problema é o cabeçalho.
    """

    def test_pede_a_midia_como_variavel(self):
        template = _FakeTemplate("Olá, {{1}}!", midia="IMAGE")
        assert variaveis_do_template(template) == ["header:media", "1"]

    def test_o_rotulo_diz_qual_midia_e(self):
        for formato, esperado in [
            ("IMAGE", "imagem do cabeçalho"),
            ("VIDEO", "vídeo do cabeçalho"),
            ("DOCUMENT", "documento do cabeçalho"),
        ]:
            template = _FakeTemplate("Oi", midia=formato)
            assert rotulo_da_variavel(template, "header:media") == esperado

    def test_o_component_sai_com_link_e_vem_ANTES_do_corpo(self):
        template = _FakeTemplate("Olá, {{1}}!", midia="IMAGE")
        components = componentes_para_a_meta(
            template, {"header:media": "https://x/banner.png", "1": "Ivanita"}
        )
        assert components[0] == {
            "type": "header",
            "parameters": [
                {"type": "image", "image": {"link": "https://x/banner.png"}}
            ],
        }
        assert [c["type"] for c in components] == ["header", "body"]

    def test_video_e_documento_usam_a_propria_chave(self):
        template = _FakeTemplate("Oi", midia="DOCUMENT")
        components = componentes_para_a_meta(
            template, {"header:media": "https://x/guia.pdf"}
        )
        assert components[0]["parameters"] == [
            {"type": "document", "document": {"link": "https://x/guia.pdf"}}
        ]

    def test_do_mapa_de_fontes_ate_o_component(self):
        """O caminho inteiro: a tela manda a FONTE, o servidor resolve."""
        template = _FakeTemplate("Olá, {{1}}!", midia="IMAGE")
        mapa = {
            "header:media": {
                "source": VariableSource.FIXED,
                "value": "https://clinica.com.br/banner.png",
            },
            "1": {"source": VariableSource.FIXED, "value": "Ivanita"},
        }
        resolvidos = parametros(template, mapa, Contexto())
        assert resolvidos["header:media"] == "https://clinica.com.br/banner.png"
        components = componentes_para_a_meta(template, resolvidos)
        assert components[0]["parameters"][0]["image"] == {
            "link": "https://clinica.com.br/banner.png"
        }

    def test_o_header_handle_do_template_NAO_vira_valor(self):
        """
        Ele é o exemplo do momento da criação, e não um id de envio: passá-lo
        faz a Meta recusar. Sem valor preenchido, não sai component nenhum -
        e é a validação que barra antes, com mensagem que diz o que falta.
        """
        template = _FakeTemplate("Oi", midia="IMAGE")
        assert componentes_para_a_meta(template, {}) is None
        assert componentes_para_a_meta(template, {"header:media": "  "}) is None


class TestOQueATelaMostra:
    """
    O rótulo e o modelo do link, que são o que a pessoa lê ao preencher.

    ⚠️ A URL do botão é FIXA até a variável (`.../agenda/{{1}}`): quem lê
    "link do botão" cola o endereço inteiro e o botão passa a apontar para
    `.../agenda/https://...`. A Meta aceita, o envio dá certo, e o link só
    falha na mão de quem clicou - defeito que nenhum teste de envio pega.
    """

    def test_o_rotulo_do_botao_pede_o_FINAL_do_link(self):
        template = _FakeTemplate(
            "Olá!",
            buttons=[
                {"type": "URL", "text": "Acessar", "url": "https://x/agenda/{{1}}"}
            ],
        )
        assert rotulo_da_variavel(template, "button:0:1") == (
            'final do link do botão "Acessar"'
        )

    def test_o_modelo_do_link_vai_junto_para_a_tela_montar_o_endereco(self):
        template = _FakeTemplate(
            "Olá!",
            buttons=[
                {"type": "URL", "text": "Acessar", "url": "https://x/agenda/{{1}}"}
            ],
        )
        assert modelo_do_link(template, "button:0:1") == "https://x/agenda/{{1}}"

    def test_copy_code_pede_codigo_e_nao_tem_link(self):
        template = _FakeTemplate(
            "Olá!", buttons=[{"type": "COPY_CODE", "text": "Copiar"}]
        )
        assert rotulo_da_variavel(template, "button:0:1") == 'código do botão "Copiar"'
        assert modelo_do_link(template, "button:0:1") == ""

    def test_a_variavel_do_corpo_continua_sendo_o_proprio_lugar_na_frase(self):
        template = _FakeTemplate("Olá, {{1}}!")
        assert rotulo_da_variavel(template, "1") == "{{1}}"
        assert modelo_do_link(template, "1") == ""


@pytest.mark.django_db
class TestOValidador:
    """
    O erro tem que aparecer na PUBLICAÇÃO, e não no envio: template com
    parâmetro faltando morre com o paciente do outro lado esperando.
    """

    def test_sem_template_escolhido_nao_publica(self, clinic_a):
        problemas = validate_graph(_grafo({}), clinic_a)
        assert any("não tem template escolhido" in p for p in problemas)

    def test_variavel_nao_preenchida_nao_publica(self, clinic_a, template):
        problemas = validate_graph(
            _grafo(
                {
                    "template_name": "retorno_paciente",
                    "variables": {"1": {"source": VariableSource.PATIENT_FIRST_NAME}},
                }
            ),
            clinic_a,
        )
        assert any("{{2}}" in p and "não foram preenchidas" in p for p in problemas)

    def test_com_todas_preenchidas_publica(self, clinic_a, template):
        problemas = validate_graph(
            _grafo(
                {
                    "template_name": "retorno_paciente",
                    "variables": {
                        "1": {"source": VariableSource.PATIENT_FIRST_NAME},
                        "2": {"source": VariableSource.FIXED, "value": "Oeiras"},
                    },
                }
            ),
            clinic_a,
        )
        assert problemas == []

    def test_template_que_nao_existe_na_conta_nao_publica(self, clinic_a):
        """
        O gestor renomeia o template na Meta e o fluxo continua apontando para
        o nome velho: sem isto, o erro só apareceria no envio.
        """
        problemas = validate_graph(
            _grafo({"template_name": "sumiu_da_meta", "variables": {}}), clinic_a
        )
        assert any("não está aprovado nesta conta" in p for p in problemas)

    def test_sem_a_clinica_ainda_pega_o_buraco_na_numeracao(self):
        problemas = validate_graph(
            _grafo(
                {
                    "template_name": "qualquer",
                    "variables": {
                        "1": {"source": VariableSource.FIXED, "value": "a"},
                        "3": {"source": VariableSource.FIXED, "value": "c"},
                    },
                }
            )
        )
        assert any("buraco na numeração" in p for p in problemas)

    def test_fonte_em_branco_nao_conta_como_preenchida(self, clinic_a, template):
        """
        ⚠️ `bool({"source": ""})` é verdadeiro: sem checar o conteúdo, a chave
        presente e vazia passaria pela publicação para morrer no envio.
        """
        problemas = validate_graph(
            _grafo(
                {
                    "template_name": "retorno_paciente",
                    "variables": {
                        "1": {"source": VariableSource.PATIENT_FIRST_NAME},
                        "2": {"source": VariableSource.FIXED, "value": "   "},
                    },
                }
            ),
            clinic_a,
        )
        assert any("{{2}}" in p for p in problemas)


@pytest.mark.django_db
def test_o_motor_manda_TEMPLATE_e_nao_texto_livre(clinic_a, template):
    """
    O coração do defeito: a mensagem precisa sair com `template_name`, senão o
    envio cai no `send_text` e a Meta recusa fora da janela de 24h.

    Roda o fluxo de ponta a ponta, e não o nó isolado: o que quebrava era a
    combinação de motor e envio, e cada lado testado sozinho passava.
    """
    from apps.automation.engine import start_run
    from apps.automation.tests.conftest import make_contact, make_conversation, make_flow

    fluxo = make_flow(
        clinic_a,
        status=FlowStatus.ACTIVE,
        graph={
            "entry_node": "n1",
            "nodes": [
                {"id": "n1", "type": FlowNodeType.START, "label": "n1", "config": {}},
                {
                    "id": "convite",
                    "type": FlowNodeType.SEND_TEMPLATE,
                    "label": "Convite de retorno",
                    "config": {
                        "template_name": "retorno_paciente",
                        "text": "Convite de retorno enviado",
                        # Texto fixo aqui: a fonte `flow_var` está coberta
                        # em `TestOsParametros`, e montar um `collect_input`
                        # antes só para isso esconderia o que este teste
                        # afirma.
                        "variables": {
                            "1": {"source": VariableSource.FIXED, "value": "Ivanita"},
                            "2": {"source": VariableSource.FIXED, "value": "Oeiras"},
                        },
                    },
                },
                {"id": "fim", "type": FlowNodeType.END, "label": "fim", "config": {}},
            ],
            "edges": [
                {"from": "n1", "to": "convite", "condition": EDGE_DEFAULT},
                {"from": "convite", "to": "fim", "condition": EDGE_DEFAULT},
            ],
        },
    )
    conversa = make_conversation(clinic_a, make_contact(clinic_a))
    run = start_run(fluxo, conversa)

    message = conversa.messages.filter(kind=MessageKind.TEMPLATE).latest("id")
    assert message.template_name == "retorno_paciente"
    assert message.content_data["template_params"] == {
        "1": "Ivanita",
        "2": "Oeiras",
    }
    # A thread mostra o corpo MONTADO, não o template cru cheio de `{{n}}`.
    assert "{{" not in message.body
    assert run is not None
