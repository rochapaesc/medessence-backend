"""
Levar um fluxo de uma clínica para outra (RF-FLW-24).

⚠️ O que estes testes protegem é o motivo do desenho: id de sequência e de
etiqueta NÃO atravessam. O mesmo número existe na clínica de destino apontando
para outra coisa, e um fluxo importado com id cru marcaria a etiqueta errada no
paciente errado. Por isso a exportação desreferencia e a importação resolve por
nome, declarando o que não achou.
"""

import pytest

from apps.automation.choices import FlowNodeType, FlowStatus
from apps.automation.models import Flow, Sequence
from apps.automation.portability import ArquivoInvalido, exportar, importar
from apps.automation.tests.conftest import make_flow
from apps.inbox.models import ConversationLabel

pytestmark = pytest.mark.django_db


def _grafo_do(flow):
    """O desenho que o fluxo importado ficou tendo."""
    versao = flow.current_version or flow.versions.order_by("-number").first()
    return versao.graph


def _grafo_com_referencias(sequence_id, label_id):
    return {
        "entry_node": "n1",
        "nodes": [
            {"id": "n1", "type": FlowNodeType.START, "label": "Início", "config": {}},
            {
                "id": "etq",
                "type": FlowNodeType.SET_LABEL,
                "label": "Marcar assunto",
                "config": {"label_id": label_id},
            },
            {
                "id": "seq",
                "type": FlowNodeType.ENROLL_SEQUENCE,
                "label": "Colocar na trilha",
                "config": {"sequence_id": sequence_id},
            },
        ],
        "edges": [
            {"from": "n1", "to": "etq", "condition": "default"},
            {"from": "etq", "to": "seq", "condition": "default"},
        ],
    }


@pytest.fixture
def fluxo_da_origem(clinic_a):
    """Um fluxo que aponta para uma sequência e uma etiqueta DA CLÍNICA A."""
    sequencia = Sequence.objects.create(clinic=clinic_a, name="Pós-consulta")
    etiqueta = ConversationLabel.objects.create(clinic=clinic_a, name="Reclamação")
    flow = make_flow(
        clinic_a,
        name="Acolhida",
        status=FlowStatus.ACTIVE,
        graph=_grafo_com_referencias(sequencia.pk, etiqueta.pk),
    )
    return flow, sequencia, etiqueta


def test_o_arquivo_leva_NOME_e_nao_id(fluxo_da_origem):
    flow, sequencia, etiqueta = fluxo_da_origem

    arquivo = exportar(flow)

    nos = {n["id"]: n["config"] for n in arquivo["grafo"]["nodes"]}
    assert nos["seq"]["sequence_name"] == "Pós-consulta"
    assert nos["etq"]["label_name"] == "Reclamação"
    # ⚠️ O id SAI do arquivo: mantê-lo junto seria deixar uma armadilha para
    # quem importasse numa clínica onde aquele número existe.
    assert "sequence_id" not in nos["seq"]
    assert "label_id" not in nos["etq"]
    # ⚠️ Sem `str(pk) not in str(arquivo)`: id curto casa por acaso dentro de
    # outro campo (o `fallback` tem números), e a asserção passaria a falhar
    # sozinha quando as sequências do banco crescessem.
    assert sequencia.pk not in nos["seq"].values()
    assert etiqueta.pk not in nos["etq"].values()


def test_importar_LIGA_pelo_nome_na_clinica_de_destino(fluxo_da_origem, clinic_b):
    """O caso feliz: a outra clínica tem os mesmos nomes, com ids diferentes."""
    flow, _, _ = fluxo_da_origem
    # Ids propositalmente diferentes dos da origem.
    outra_sequencia = Sequence.objects.create(clinic=clinic_b, name="Pós-consulta")
    outra_etiqueta = ConversationLabel.objects.create(clinic=clinic_b, name="Reclamação")

    novo, pendencias = importar(clinic_b, exportar(flow))

    nos = {n["id"]: n["config"] for n in _grafo_do(novo)["nodes"]}
    assert nos["seq"]["sequence_id"] == outra_sequencia.pk
    assert nos["etq"]["label_id"] == outra_etiqueta.pk
    assert pendencias == []


def test_o_que_NAO_existe_la_vira_pendencia_declarada(fluxo_da_origem, clinic_b):
    """
    ⚠️ O nó FICA, com o campo vazio: apagá-lo mudaria o desenho por baixo dos
    panos, e quem importou não saberia que o fluxo ficou diferente.
    """
    flow, _, _ = fluxo_da_origem
    ConversationLabel.objects.create(clinic=clinic_b, name="Reclamação")

    novo, pendencias = importar(clinic_b, exportar(flow))

    nos = {n["id"]: n["config"] for n in _grafo_do(novo)["nodes"]}
    assert "sequence_id" not in nos["seq"], "não inventa id nenhum"
    assert len(pendencias) == 1
    assert "Pós-consulta" in pendencias[0]
    assert "Colocar na trilha" in pendencias[0], "diz QUAL passo, não só o nome"


def test_o_fluxo_importado_nasce_em_RASCUNHO(fluxo_da_origem, clinic_b):
    """Publicar sozinho poria no ar um fluxo que fala com paciente sem ninguém
    ter olhado — e o arquivo pode citar coisa que não existe aqui."""
    flow, _, _ = fluxo_da_origem

    novo, _ = importar(clinic_b, exportar(flow))

    assert novo.status == FlowStatus.DRAFT
    assert novo.clinic_id == clinic_b.pk


def test_nome_diferente_de_caixa_ainda_casa(fluxo_da_origem, clinic_b):
    """Quem cadastrou "reclamação" de um lado e "Reclamação" do outro quis
    dizer a mesma coisa."""
    flow, _, _ = fluxo_da_origem
    Sequence.objects.create(clinic=clinic_b, name="pós-consulta")
    etiqueta = ConversationLabel.objects.create(clinic=clinic_b, name="RECLAMAÇÃO")

    novo, pendencias = importar(clinic_b, exportar(flow))

    nos = {n["id"]: n["config"] for n in _grafo_do(novo)["nodes"]}
    assert nos["etq"]["label_id"] == etiqueta.pk
    assert pendencias == []


def test_modelo_de_mensagem_vira_aviso_para_conferir(clinic_a, clinic_b):
    """
    O template viaja por nome (é assim que a Meta o identifica), mas a clínica
    de destino tem OUTRA conta na Meta: ele pode não existir aprovado lá.
    """
    flow = make_flow(
        clinic_a,
        name="Resgate",
        graph={
            "entry_node": "n1",
            "nodes": [
                {"id": "n1", "type": FlowNodeType.START, "label": "Início", "config": {}},
                {
                    "id": "tpl",
                    "type": FlowNodeType.SEND_TEMPLATE,
                    "label": "Convite",
                    "config": {"template_name": "convite_retorno"},
                },
            ],
            "edges": [{"from": "n1", "to": "tpl", "condition": "default"}],
        },
    )

    _, pendencias = importar(clinic_b, exportar(flow))

    assert len(pendencias) == 1
    assert "convite_retorno" in pendencias[0]
    assert "aprovado" in pendencias[0]


def test_arquivo_que_nao_e_nosso_e_recusado_com_frase(clinic_b):
    with pytest.raises(ArquivoInvalido) as erro:
        importar(clinic_b, {"qualquer": "coisa"})
    assert "não parece um fluxo exportado" in str(erro.value)


def test_formato_de_outra_versao_e_recusado(clinic_b):
    """Arquivo de uma versão futura tem de falar, não ser lido pela metade."""
    with pytest.raises(ArquivoInvalido) as erro:
        importar(clinic_b, {"formato": 99, "grafo": {}})
    assert "versão" in str(erro.value)


def test_importar_NAO_alcanca_outra_clinica(clinic_a, clinic_b, fluxo_da_origem):
    """A importação escreve na clínica pedida, e só nela."""
    flow, _, _ = fluxo_da_origem
    antes = Flow.objects.filter(clinic=clinic_a).count()

    importar(clinic_b, exportar(flow))

    assert Flow.objects.filter(clinic=clinic_a).count() == antes
