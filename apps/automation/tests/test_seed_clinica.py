"""
O fluxo de recepção modelado no atendimento REAL da clínica.

Diferente do `--completo`, que existe para exercitar o catálogo, este existe
para provar que um atendimento plausível de ponta a ponta se sustenta: as duas
pontas do horário, os três caminhos do menu, a variável coletada aparecendo na
fala seguinte e os dois desfechos possíveis (com gente e sem gente).
"""

import pytest
from django.core.management import call_command

from apps.automation.choices import FlowNodeType, FlowStatus
from apps.automation.graph import FlowGraph, validate_graph
from apps.automation.models import Flow
from apps.inbox.models import ConversationLabel

pytestmark = pytest.mark.django_db

NOME = "Atendimento da recepção"


@pytest.fixture
def etiquetas(clinic_a):
    return {
        nome: ConversationLabel.objects.create(clinic=clinic_a, name=nome)
        for nome in ("Agendamento", "Convênio", "Reagendamento")
    }


@pytest.fixture
def flow(clinic_a, etiquetas):
    call_command("seed_flow_clinica", clinic=clinic_a.pk)
    return Flow.objects.get(clinic=clinic_a, name=NOME)


@pytest.fixture
def grafo(flow):
    return flow.current_version.graph


@pytest.fixture
def g(grafo):
    return FlowGraph(grafo)


def _no(grafo, node_id):
    return next(n for n in grafo["nodes"] if n["id"] == node_id)


class TestOFluxoSeSustenta:
    def test_passa_no_validador(self, grafo):
        assert validate_graph(grafo) == []

    def test_nasce_em_rascunho(self, flow):
        """Fluxo ativo responde no lugar da clínica para todo mundo que
        escrever. Publicar é decisão de quem opera."""
        assert flow.status == FlowStatus.DRAFT

    def test_nao_usa_nenhum_no_travado_por_pendencia(self, grafo):
        """P14 (IA) e P15 (HTTP)."""
        usados = {n["type"] for n in grafo["nodes"]}

        assert not usados & {"llm_agent", "transcribe_audio", "http_request"}

    def test_etiqueta_e_ConversationLabel_e_nao_Tag(self, grafo, etiquetas):
        """
        RF-FLW-13.1: a `patients.Tag` sincroniza com a vSaúde, e um fluxo
        marcando "convênio" tentaria escrever no prontuário do paciente.
        """
        ids = {n["config"]["label_id"] for n in grafo["nodes"] if n["type"] == "set_label"}

        assert ids == {e.pk for e in etiquetas.values()}


class TestAsDuasPontasDoHorario:
    """
    O primeiro nó do fluxo pergunta se a clínica está aberta. É o RF-FLW-5.1
    dentro do desenho, e não como marca do fluxo: assim o paciente recebe
    resposta nos dois casos.
    """

    def test_o_primeiro_passo_pergunta_o_horario(self, grafo, g):
        primeiro = g.resolve(grafo["entry_node"], "default")

        assert primeiro == "esta_aberta"
        assert _no(grafo, "esta_aberta")["config"]["subject"] == "business_hours"

    def test_aberta_entrega_para_a_recepcao_sem_menu(self, grafo, g):
        """Dentro do expediente ninguém quer conversar com robô."""
        depois = g.resolve("esta_aberta", "true")

        assert _no(grafo, depois)["type"] == FlowNodeType.SEND_MESSAGE
        assert _no(grafo, g.resolve(depois, "default"))["type"] == FlowNodeType.HANDOFF

    def test_fechada_cai_no_menu(self, grafo, g):
        saudacao = g.resolve("esta_aberta", "false")

        assert _no(grafo, g.resolve(saudacao, "default"))["type"] == FlowNodeType.SEND_BUTTONS

    def test_o_fluxo_NAO_e_marcado_como_so_fora_do_horario(self, flow):
        """
        Se fosse, ele nem dispararia dentro do expediente e o paciente ficaria
        sem nenhuma resposta até alguém abrir o Inbox. Quem decide o que dizer
        é o desenho.
        """
        assert flow.only_outside_hours is False


class TestOsTresCaminhosDoMenu:
    def test_o_menu_cabe_no_limite_da_meta(self, grafo):
        """Mais de 3 botões é RECUSADO no envio, com o fluxo já publicado."""
        assert len(_no(grafo, "menu")["config"]["buttons"]) <= 3

    def test_a_lista_cabe_no_limite_da_meta(self, grafo):
        assert len(_no(grafo, "tipo_atendimento")["config"]["rows"]) <= 10

    def test_todo_botao_do_menu_tem_para_onde_ir(self, grafo, g):
        for botao in _no(grafo, "menu")["config"]["buttons"]:
            assert g.resolve("menu", f"button:{botao['id']}"), botao["id"]

    def test_todo_tipo_de_atendimento_leva_a_coleta_do_nome(self, grafo, g):
        """A recepção é quem monta a agenda; o tipo escolhido já ficou na
        conversa."""
        for linha in _no(grafo, "tipo_atendimento")["config"]["rows"]:
            assert g.resolve("tipo_atendimento", f"row:{linha['id']}") == "coleta_nome"


class TestOQueOPacienteResponde:
    def test_o_nome_coletado_reaparece_na_pergunta_seguinte(self, grafo, g):
        """Sem isto o fluxo pergunta o nome e nunca mais o usa, que é a
        primeira coisa que denuncia robô."""
        seguinte = _no(grafo, g.resolve("coleta_nome", "default"))

        assert seguinte["id"] == "forma_pagamento"
        assert "{{nome}}" in seguinte["config"]["text"]

    def test_so_e_texto_livre_o_que_e_mesmo_aberto(self, grafo):
        """
        Resposta de conjunto FECHADO se escolhe, não se digita (06/08/2026).
        A forma de pagamento era pergunta aberta e começava com "você tem
        convênio?": o paciente respondia "Tenho" e a recepção recebia
        `Pagamento: Tenho`.
        """
        abertos = {
            n["config"]["var_key"]
            for n in grafo["nodes"]
            if n["type"] == FlowNodeType.COLLECT_INPUT
        }

        assert abertos == {"nome", "assunto"}, "nome e assunto não têm lista possível"

    def test_a_forma_de_pagamento_e_botao_e_guarda_a_escolha(self, grafo):
        no = _no(grafo, "forma_pagamento")

        assert no["type"] == FlowNodeType.SEND_BUTTONS
        assert no["config"]["var_key"] == "pagamento"
        assert {b["id"] for b in no["config"]["buttons"]} == {"particular", "convenio"}

    def test_o_tipo_de_atendimento_tambem_e_guardado(self, grafo):
        """É o dado de que quem agenda mais precisa, e ele não chegava na nota
        de jeito nenhum: a lista não guardava a escolha."""
        assert _no(grafo, "tipo_atendimento")["config"]["var_key"] == "atendimento"

    def test_particular_e_convenio_seguem_caminhos_diferentes(self, grafo, g):
        particular = g.resolve("forma_pagamento", "button:particular")
        convenio = g.resolve("forma_pagamento", "button:convenio")

        assert particular != convenio
        # Só o caminho do convênio etiqueta, porque só ele muda o que a
        # recepção precisa conferir antes de confirmar.
        assert g.resolve(convenio, "default") == "marca_convenio"

    def test_os_dois_caminhos_se_reencontram_no_resumo(self, grafo, g):
        """Pedido de agendamento é pedido de agendamento, pague como pagar."""
        por_particular = g.resolve(g.resolve("forma_pagamento", "button:particular"), "default")
        info = g.resolve("forma_pagamento", "button:convenio")
        por_convenio = g.resolve(g.resolve(info, "default"), "default")

        assert por_particular == por_convenio == "resumo"

    def test_a_etiqueta_vem_DEPOIS_da_fala_e_antes_de_entregar(self, grafo, g):
        """
        Pedido do usuário em 06/08/2026, e a razão é de produto: a etiqueta é
        o registro do que aconteceu. Marcada antes de o robô falar, o paciente
        que abandona no meio deixa a conversa etiquetada com um pedido que
        nunca se formou. O ramo de remarcação era o pior: etiquetava no clique
        do menu, antes até de perguntar o nome.
        """
        etiquetas = [n["id"] for n in grafo["nodes"] if n["type"] == FlowNodeType.SET_LABEL]

        for marca in etiquetas:
            anterior = next(e["from"] for e in grafo["edges"] if e["to"] == marca)
            assert _no(grafo, anterior)["type"] in (
                FlowNodeType.SEND_MESSAGE,
                FlowNodeType.SEND_BUTTONS,
                FlowNodeType.SEND_LIST,
            ), f"{marca} não vem depois de uma fala"

        # E a última etiqueta de cada ramo encosta na entrega.
        assert g.resolve("marca_agendamento", "default") == "recepcao_agenda"
        assert g.resolve("marca_reagendamento", "default") == "recepcao_remarcar"

    def test_o_ramo_de_remarcar_NAO_etiqueta_no_clique_do_menu(self, grafo, g):
        """Tocar em "Remarcar" e sumir não pode deixar a marca para trás."""
        primeiro = g.resolve("menu", "button:remarcar")

        assert _no(grafo, primeiro)["type"] == FlowNodeType.COLLECT_INPUT

    def test_a_nota_para_a_recepcao_leva_o_que_foi_coletado(self, grafo):
        """Quem abre a conversa de manhã precisa saber quem é, o que quer e
        como paga sem ter de reler a thread inteira."""
        nota = _no(grafo, "recepcao_agenda")["config"]["note"]

        assert "{{nome}}" in nota
        assert "{{atendimento}}" in nota
        assert "{{pagamento}}" in nota


class TestOsDoisDesfechos:
    def test_quem_quer_retorno_vai_para_a_recepcao(self, grafo, g):
        destino = g.resolve("precisa_retorno", "button:sim")

        assert _no(grafo, g.resolve(destino, "default"))["type"] == FlowNodeType.HANDOFF

    def test_quem_nao_quer_retorno_encerra_sem_ocupar_a_fila(self, grafo, g):
        """
        O único caminho em que o robô resolve sozinho. Sem ele, quem só queria
        deixar um recado entra na fila igual, e a recepção abre a manhã com
        conversa que já acabou.
        """
        despedida = g.resolve("precisa_retorno", "button:nao")

        assert _no(grafo, g.resolve(despedida, "default"))["type"] == FlowNodeType.END

    def test_todo_caminho_termina_em_gente_ou_em_fim(self, grafo, g):
        """Nó sem saída no meio do fluxo deixaria o paciente falando sozinho."""
        finais = {FlowNodeType.HANDOFF, FlowNodeType.END}
        for node in grafo["nodes"]:
            if node["type"] in finais:
                continue
            saidas = [e for e in grafo["edges"] if e["from"] == node["id"]]
            assert saidas, f"{node['id']} não leva a lugar nenhum"


class TestSemearDeNovo:
    def test_semear_duas_vezes_versiona_em_vez_de_duplicar(self, clinic_a, etiquetas, flow):
        """
        RF-FLW-1.1: quem está no meio do fluxo continua na versão em que
        entrou, então semear de novo não pode apagar a versão anterior.
        """
        call_command("seed_flow_clinica", clinic=clinic_a.pk)

        assert Flow.objects.filter(clinic=clinic_a, name=NOME).count() == 1
        flow.refresh_from_db()
        assert flow.current_version.number == 2
        assert flow.versions.count() == 2

    def test_semear_de_novo_NAO_desfaz_a_politica_ajustada_na_tela(
        self, clinic_a, etiquetas, flow
    ):
        """
        ⚠️ Semear é atualizar o DESENHO. O `update_or_create` com defaults
        desfazia em silêncio o gatilho, a prioridade e o fallback que alguém
        tinha ajustado pela gaveta, e o fluxo parava de disparar sem nada no
        log dizendo por quê. Aconteceu no teste ao vivo de 06/08/2026: a
        palavra-chave configurada virou `primeira mensagem` de volta.
        """
        from apps.automation.choices import FlowTrigger

        flow.trigger = FlowTrigger.KEYWORD
        flow.trigger_config = {"match": "exact", "keywords": ["fluxoclinica"]}
        flow.priority = 1
        flow.only_outside_hours = True
        flow.save()

        call_command("seed_flow_clinica", clinic=clinic_a.pk)

        flow.refresh_from_db()
        assert flow.trigger == FlowTrigger.KEYWORD
        assert flow.trigger_config == {"match": "exact", "keywords": ["fluxoclinica"]}
        assert flow.priority == 1
        assert flow.only_outside_hours is True
        assert flow.current_version.number == 2, "mas o DESENHO foi atualizado"

    def test_sem_as_etiquetas_o_comando_recusa_em_vez_de_semear_quebrado(self, clinic_a):
        """
        `set_label` guarda o id: etiqueta ausente faria o nó falhar em silêncio
        no meio do atendimento de um paciente de verdade.
        """
        from django.core.management.base import CommandError

        with pytest.raises(CommandError, match="etiquetas"):
            call_command("seed_flow_clinica", clinic=clinic_a.pk)
