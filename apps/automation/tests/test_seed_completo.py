"""
O fluxo de validação (`--completo`): o que prova que o catálogo inteiro se
sustenta, do validador ao canvas.
"""

import pytest
from django.core.management import call_command

from apps.automation.choices import FlowNodeType, FlowStatus
from apps.automation.graph import FlowGraph, validate_graph
from apps.automation.models import Flow

pytestmark = pytest.mark.django_db

NOME = "Validação completa (todos os passos)"


@pytest.fixture
def grafo_completo(clinic_a):
    call_command("seed_flow_demo", clinic=clinic_a.pk, completo=True)
    return Flow.objects.get(clinic=clinic_a, name=NOME).current_version.graph


def test_usa_os_doze_tipos_do_catalogo(grafo_completo):
    """
    É o motivo de este fluxo existir: um tipo que ninguém exercita é um tipo
    que quebra na primeira vez que alguém usar.
    """
    usados = {n["type"] for n in grafo_completo["nodes"]}

    assert usados == set(FlowNodeType.values)


def test_nao_tem_nenhum_no_travado_por_pendencia(grafo_completo):
    """
    P14 (IA): nem de propósito num fluxo de teste.

    ⚠️ `http_request` SAIU desta lista em 20/08/2026, quando o usuário
    aprovou a cerca do RF-FLW-16.1 e a P15 deixou de bloquear. Os de IA
    continuam: a P14 é base legal e contrato de operador, e nada aqui a
    resolve.
    """
    usados = {n["type"] for n in grafo_completo["nodes"]}

    assert not usados & {"llm_agent", "transcribe_audio"}


def test_o_no_http_do_seed_aponta_para_um_cadastro_desligado(grafo_completo):
    """
    ⚠️ O nó de exemplo não pode sair chamando endereço nenhum.

    O destino semeado é inventado (`exemplo.com`), e um fluxo de demonstração
    que o chamasse a cada agendamento encheria o log da clínica de falha de
    rede - além de ser uma chamada de saída que ninguém pediu.
    """
    from apps.automation.models import HttpDestination

    no = next(n for n in grafo_completo["nodes"] if n["type"] == "http_request")
    destino = HttpDestination.objects.get(pk=no["config"]["destination_id"])

    assert destino.is_active is False


def test_passa_no_validador(grafo_completo):
    assert validate_graph(grafo_completo) == []


def test_toda_saida_esta_ligada(grafo_completo):
    """
    O validador já cobre isso, mas aqui a falha aponta QUAL passo ficou solto,
    que é o que se quer saber quando o teste vermelho aparece.
    """
    from apps.automation.graph import required_conditions

    g = FlowGraph(grafo_completo)
    for node in g.nodes:
        faltando = required_conditions(node) - g.conditions_of(node.id)
        assert not faltando, f'"{node.label}" ficou com {faltando} sem ligação'


def test_ramifica_de_verdade(grafo_completo):
    """
    Fluxo em linha reta não exercita porta nenhuma. Este tem que ter passo com
    mais de uma saída e passo que recebe de mais de um lugar.
    """
    g = FlowGraph(grafo_completo)

    com_varias_saidas = [n for n in g.nodes if len(g.outgoing(n.id)) > 1]
    destinos = [e.to_id for n in g.nodes for e in g.outgoing(n.id)]
    com_varias_entradas = {d for d in destinos if destinos.count(d) > 1}

    assert len(com_varias_saidas) >= 3
    assert com_varias_entradas


def test_o_horario_de_funcionamento_e_a_primeira_pergunta(grafo_completo):
    """
    RF-FLW-5.1 na prática: antes de qualquer coisa, o fluxo pergunta se a
    clínica está aberta, e manda para a recepção quando está.
    """
    g = FlowGraph(grafo_completo)
    primeiro = g.node(g.resolve(g.entry_node, "default"))

    assert primeiro.type == FlowNodeType.CONDITION
    assert primeiro.config["subject"] == "business_hours"
    assert g.node(g.resolve(primeiro.id, "true")).type == FlowNodeType.HANDOFF


def test_nasce_em_rascunho(clinic_a, grafo_completo):
    """Ninguém publica fluxo de teste por engano."""
    assert Flow.objects.get(clinic=clinic_a, name=NOME).status == FlowStatus.DRAFT


def test_o_de_demonstracao_e_o_completo_convivem(clinic_a):
    call_command("seed_flow_demo", clinic=clinic_a.pk)
    call_command("seed_flow_demo", clinic=clinic_a.pk, completo=True)

    assert Flow.objects.filter(clinic=clinic_a).count() == 2
