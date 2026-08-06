from apps.automation.choices import ConditionOperator, ConditionSubject, FlowNodeType
from apps.automation.graph import FlowGraph, required_conditions, validate_graph

# ---- helpers de montagem ----


def node(node_id, tipo, **config):
    return {"id": node_id, "type": tipo, "label": node_id, "config": config}


def edge(origem, destino, condition="default"):
    return {"from": origem, "to": destino, "condition": condition}


def graph(nodes, edges, entry="n1"):
    return {"entry_node": entry, "nodes": nodes, "edges": edges}


def mensagem(node_id, texto="Olá"):
    return node(node_id, FlowNodeType.SEND_MESSAGE, text=texto)


def fluxo_minimo_valido():
    """Início → mensagem → fim. O menor fluxo que passa na validação."""
    return graph(
        [node("n1", FlowNodeType.START), mensagem("n2"), node("n3", FlowNodeType.END)],
        [edge("n1", "n2"), edge("n2", "n3")],
    )


class TestResolucaoDeAresta:
    """RF-FLW-2: exata vence, senão `default`, senão o ramo acaba."""

    def test_condicao_exata_vence(self):
        g = FlowGraph(
            graph(
                [node("n1", FlowNodeType.CONDITION), mensagem("sim"), mensagem("nao")],
                [edge("n1", "sim", "true"), edge("n1", "nao", "false")],
            )
        )

        assert g.resolve("n1", "true") == "sim"
        assert g.resolve("n1", "false") == "nao"

    def test_sem_condicao_exata_cai_no_default(self):
        g = FlowGraph(
            graph(
                [node("n1", FlowNodeType.SEND_BUTTONS), mensagem("n2")],
                [edge("n1", "n2", "default")],
            )
        )

        assert g.resolve("n1", "button:qualquer_coisa") == "n2"

    def test_o_default_nao_atropela_a_exata_mesmo_vindo_antes(self):
        """
        A ordem das arestas no JSON é a ordem em que o gestor ligou os nós, e
        não pode decidir o roteamento.
        """
        g = FlowGraph(
            graph(
                [node("n1", FlowNodeType.SEND_BUTTONS), mensagem("cai"), mensagem("certo")],
                [edge("n1", "cai", "default"), edge("n1", "certo", "button:sim")],
            )
        )

        assert g.resolve("n1", "button:sim") == "certo"

    def test_sem_saida_nenhuma_o_ramo_termina(self):
        g = FlowGraph(fluxo_minimo_valido())

        assert g.resolve("n3", "default") is None

    def test_no_inexistente_nao_estoura(self):
        g = FlowGraph(fluxo_minimo_valido())

        assert g.resolve("nao_existe", "default") is None

    def test_grafo_vazio_nao_estoura(self):
        g = FlowGraph({})

        assert g.nodes == []
        assert g.resolve("n1", "default") is None


class TestSaidasObrigatorias:
    def test_condicao_precisa_de_verdadeiro_e_falso(self):
        n = FlowGraph(graph([node("n1", FlowNodeType.CONDITION)], [])).node("n1")

        assert required_conditions(n) == {"true", "false"}

    def test_botoes_geram_uma_saida_por_botao(self):
        n = FlowGraph(
            graph(
                [
                    node(
                        "n1",
                        FlowNodeType.SEND_BUTTONS,
                        buttons=[{"id": "sim", "title": "Sim"}, {"id": "nao", "title": "Não"}],
                    )
                ],
                [],
            )
        ).node("n1")

        assert required_conditions(n) == {"button:sim", "button:nao"}

    def test_no_terminal_nao_tem_saida(self):
        for tipo in (FlowNodeType.END, FlowNodeType.HANDOFF):
            n = FlowGraph(graph([node("n1", tipo)], [])).node("n1")

            assert required_conditions(n) == set()

    def test_o_resto_precisa_de_um_default(self):
        n = FlowGraph(graph([mensagem("n1")], [])).node("n1")

        assert required_conditions(n) == {"default"}


class TestValidacao:
    def test_fluxo_minimo_passa(self):
        assert validate_graph(fluxo_minimo_valido()) == []

    def test_fluxo_vazio_e_recusado(self):
        assert validate_graph({}) == ["O fluxo está vazio."]

    def test_sem_no_de_inicio(self):
        problemas = validate_graph(
            graph([mensagem("n1"), node("n2", FlowNodeType.END)], [edge("n1", "n2")])
        )

        assert any("início" in p for p in problemas)

    def test_entrada_que_nao_e_no_de_inicio(self):
        problemas = validate_graph(
            graph(
                [node("n1", FlowNodeType.START), mensagem("n2"), node("n3", FlowNodeType.END)],
                [edge("n2", "n3")],
                entry="n2",
            )
        )

        assert any("começar por um nó de início" in p for p in problemas)

    def test_saida_sem_ligacao_e_recusada(self):
        """Botão que não leva a lugar nenhum: o paciente clica e some."""
        problemas = validate_graph(
            graph(
                [
                    node("n1", FlowNodeType.START),
                    node(
                        "n2",
                        FlowNodeType.SEND_BUTTONS,
                        text="Escolha",
                        buttons=[{"id": "sim", "title": "Sim"}, {"id": "nao", "title": "Não"}],
                    ),
                    node("n3", FlowNodeType.END),
                ],
                [edge("n1", "n2"), edge("n2", "n3", "button:sim")],
            )
        )

        assert any("saída(s) sem ligação" in p for p in problemas)

    def test_ligacao_para_no_inexistente(self):
        problemas = validate_graph(
            graph(
                [node("n1", FlowNodeType.START), mensagem("n2")],
                [edge("n1", "n2"), edge("n2", "fantasma")],
            )
        )

        assert any("não chega a lugar nenhum" in p for p in problemas)

    def test_no_orfao_e_apontado(self):
        problemas = validate_graph(
            graph(
                [
                    node("n1", FlowNodeType.START),
                    mensagem("n2"),
                    node("n3", FlowNodeType.END),
                    mensagem("perdido", "ninguém chega aqui"),
                ],
                [edge("n1", "n2"), edge("n2", "n3"), edge("perdido", "n3")],
            )
        )

        assert any("nunca chega em" in p and "perdido" in p for p in problemas)

    def test_no_terminal_com_saida_e_recusado(self):
        problemas = validate_graph(
            graph(
                [node("n1", FlowNodeType.START), node("n2", FlowNodeType.END), mensagem("n3")],
                [edge("n1", "n2"), edge("n2", "n3")],
            )
        )

        assert any("não pode ter saída" in p for p in problemas)


class TestLacoInfinito:
    """
    O defeito mais caro: o motor gira e o WhatsApp bloqueia o número da
    clínica por spam. O que quebra o laço é um nó que ESPERA o paciente.
    """

    def test_laco_sem_espera_e_recusado(self):
        problemas = validate_graph(
            graph(
                [node("n1", FlowNodeType.START), mensagem("n2"), mensagem("n3")],
                [edge("n1", "n2"), edge("n2", "n3"), edge("n3", "n2")],
            )
        )

        assert any("sem esperar o paciente responder" in p for p in problemas)

    def test_laco_com_coleta_no_meio_e_aceito(self):
        """
        Repetir a pergunta até o paciente acertar é desenho legítimo, e é o
        que a política de reprompt faz.
        """
        problemas = validate_graph(
            graph(
                [
                    node("n1", FlowNodeType.START),
                    node(
                        "n2",
                        FlowNodeType.COLLECT_INPUT,
                        prompt_text="Qual a data?",
                        var_key="data",
                    ),
                    mensagem("n3", "Não entendi"),
                ],
                [edge("n1", "n2"), edge("n2", "n3"), edge("n3", "n2")],
            )
        )

        assert problemas == []

    def test_laco_com_espera_de_tempo_e_aceito(self):
        problemas = validate_graph(
            graph(
                [
                    node("n1", FlowNodeType.START),
                    node("n2", FlowNodeType.WAIT, amount=1, unit="days"),
                    mensagem("n3", "Ainda por aí?"),
                ],
                [edge("n1", "n2"), edge("n2", "n3"), edge("n3", "n2")],
            )
        )

        assert problemas == []

    def test_laco_longo_sem_espera_tambem_e_pego(self):
        problemas = validate_graph(
            graph(
                [
                    node("n1", FlowNodeType.START),
                    mensagem("a"),
                    mensagem("b"),
                    mensagem("c"),
                ],
                [edge("n1", "a"), edge("a", "b"), edge("b", "c"), edge("c", "a")],
            )
        )

        assert any("sem esperar o paciente responder" in p for p in problemas)


class TestConfiguracaoDosNos:
    def test_mensagem_sem_texto(self):
        problemas = validate_graph(
            graph(
                [node("n1", FlowNodeType.START), mensagem("n2", ""), node("n3", FlowNodeType.END)],
                [edge("n1", "n2"), edge("n2", "n3")],
            )
        )

        assert any("não tem mensagem para enviar" in p for p in problemas)

    def test_mais_de_tres_botoes_e_recusado(self):
        """Teto da Meta: publicar assim faria o envio ser recusado em produção."""
        botoes = [{"id": f"b{i}", "title": f"Opção {i}"} for i in range(4)]
        problemas = validate_graph(
            graph(
                [
                    node("n1", FlowNodeType.START),
                    node("n2", FlowNodeType.SEND_BUTTONS, text="Escolha", buttons=botoes),
                    node("n3", FlowNodeType.END),
                ],
                [edge("n1", "n2")] + [edge("n2", "n3", f"button:b{i}") for i in range(4)],
            )
        )

        assert any("no máximo 3" in p for p in problemas)

    def test_mais_de_dez_itens_de_lista_e_recusado(self):
        linhas = [{"id": f"r{i}", "title": f"Item {i}"} for i in range(11)]
        problemas = validate_graph(
            graph(
                [
                    node("n1", FlowNodeType.START),
                    node("n2", FlowNodeType.SEND_LIST, text="Escolha", rows=linhas),
                    node("n3", FlowNodeType.END),
                ],
                [edge("n1", "n2")] + [edge("n2", "n3", f"row:r{i}") for i in range(11)],
            )
        )

        assert any("no máximo 10" in p for p in problemas)

    def test_coleta_sem_variavel(self):
        problemas = validate_graph(
            graph(
                [
                    node("n1", FlowNodeType.START),
                    node("n2", FlowNodeType.COLLECT_INPUT, prompt_text="Qual a data?"),
                    node("n3", FlowNodeType.END),
                ],
                [edge("n1", "n2"), edge("n2", "n3")],
            )
        )

        assert any("em que variável guardar" in p for p in problemas)

    def test_condicao_de_variavel_precisa_dizer_qual(self):
        problemas = validate_graph(
            graph(
                [
                    node("n1", FlowNodeType.START),
                    node(
                        "n2",
                        FlowNodeType.CONDITION,
                        subject=ConditionSubject.VAR,
                        operator=ConditionOperator.EQUALS,
                        value="sim",
                    ),
                    node("n3", FlowNodeType.END),
                ],
                [edge("n1", "n2"), edge("n2", "n3", "true"), edge("n2", "n3", "false")],
            )
        )

        assert any("qual variável comparar" in p for p in problemas)

    def test_condicao_de_horario_nao_precisa_de_variavel(self):
        """
        `business_hours` não compara variável nenhuma: pergunta se a clínica
        está aberta agora (RF-FLW-5.1).
        """
        problemas = validate_graph(
            graph(
                [
                    node("n1", FlowNodeType.START),
                    node(
                        "n2",
                        FlowNodeType.CONDITION,
                        subject=ConditionSubject.BUSINESS_HOURS,
                        operator=ConditionOperator.PRESENT,
                    ),
                    node("n3", FlowNodeType.END),
                ],
                [edge("n1", "n2"), edge("n2", "n3", "true"), edge("n2", "n3", "false")],
            )
        )

        assert problemas == []

    def test_operador_de_comparacao_sem_valor(self):
        problemas = validate_graph(
            graph(
                [
                    node("n1", FlowNodeType.START),
                    node(
                        "n2",
                        FlowNodeType.CONDITION,
                        subject=ConditionSubject.VAR,
                        subject_key="opcao",
                        operator=ConditionOperator.EQUALS,
                    ),
                    node("n3", FlowNodeType.END),
                ],
                [edge("n1", "n2"), edge("n2", "n3", "true"), edge("n2", "n3", "false")],
            )
        )

        assert any("não tem valor para comparar" in p for p in problemas)

    def test_espera_sem_tempo(self):
        problemas = validate_graph(
            graph(
                [
                    node("n1", FlowNodeType.START),
                    node("n2", FlowNodeType.WAIT, amount=0, unit="minutes"),
                    node("n3", FlowNodeType.END),
                ],
                [edge("n1", "n2"), edge("n2", "n3")],
            )
        )

        assert any("não tem tempo de espera" in p for p in problemas)

    def test_varios_problemas_saem_juntos(self):
        """
        O gestor conserta tudo de uma vez em vez de descobrir um erro por
        tentativa de ativar.
        """
        problemas = validate_graph(
            graph(
                [node("n1", FlowNodeType.START), mensagem("n2", ""), mensagem("orfao", "")],
                [edge("n1", "n2")],
            )
        )

        assert len(problemas) >= 3
