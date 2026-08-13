"""
A forma do grafo e como se anda nele (§4.3.2, RF-FLW-1 e RF-FLW-2).

Lógica pura: não toca banco, não envia nada. O motor (fatia 3) usa isto para
decidir para onde ir; o validador, abaixo, usa para recusar a ativação de um
desenho que travaria com o paciente do outro lado.

Formato do `FlowVersion.graph`:

    {
      "entry_node": "n1",
      "nodes": [{"id": "n1", "type": "start", "label": "", "config": {},
                 "position": {"x": 0, "y": 0}}],
      "edges": [{"from": "n1", "to": "n2", "condition": "default"}]
    }

`position` é do canvas (fatia 6) e o motor ignora - fica no mesmo JSON para
mover um nó não virar escrita em outra tabela.
"""

from dataclasses import dataclass, field

from apps.automation.choices import (
    AWAITING_NODE_TYPES,
    EDGE_BUTTON_PREFIX,
    EDGE_DEFAULT,
    EDGE_FALSE,
    EDGE_ROW_PREFIX,
    EDGE_TRUE,
    MAX_BUTTONS,
    MAX_LIST_ROWS,
    OPERATORS_WITH_VALUE,
    TERMINAL_NODE_TYPES,
    ConditionOperator,
    ConditionSubject,
    FlowNodeType,
)


@dataclass(frozen=True)
class Node:
    id: str
    type: str
    label: str = ""
    config: dict = field(default_factory=dict)

    @property
    def is_terminal(self) -> bool:
        return self.type in TERMINAL_NODE_TYPES

    @property
    def awaits_contact(self) -> bool:
        """Para e espera o paciente falar. Base da detecção de laço infinito."""
        return self.type in AWAITING_NODE_TYPES


@dataclass(frozen=True)
class Edge:
    from_id: str
    to_id: str
    condition: str = EDGE_DEFAULT


class FlowGraph:
    """
    O grafo carregado, com os índices que o motor consulta a cada avanço.

    Construir é barato e acontece uma vez por avanço; guardar o objeto entre
    requisições não vale o risco de servir desenho velho.
    """

    def __init__(self, raw: dict | None = None):
        raw = raw or {}
        self.entry_node: str = raw.get("entry_node") or ""
        self._nodes: dict[str, Node] = {}
        self._out: dict[str, list[Edge]] = {}
        self._targets: set[str] = set()

        for item in raw.get("nodes") or []:
            node = Node(
                id=str(item.get("id") or ""),
                type=str(item.get("type") or ""),
                label=str(item.get("label") or ""),
                config=item.get("config") or {},
            )
            if node.id:
                self._nodes[node.id] = node

        for item in raw.get("edges") or []:
            edge = Edge(
                from_id=str(item.get("from") or ""),
                to_id=str(item.get("to") or ""),
                condition=str(item.get("condition") or EDGE_DEFAULT),
            )
            self._out.setdefault(edge.from_id, []).append(edge)
            self._targets.add(edge.to_id)

    # ---- leitura ----

    @property
    def nodes(self) -> list[Node]:
        return list(self._nodes.values())

    def node(self, node_id: str) -> Node | None:
        return self._nodes.get(node_id)

    def outgoing(self, node_id: str) -> list[Edge]:
        return self._out.get(node_id, [])

    def conditions_of(self, node_id: str) -> set[str]:
        return {e.condition for e in self.outgoing(node_id)}

    def resolve(self, node_id: str, outcome: str) -> str | None:
        """
        Para onde ir saindo de `node_id` com o resultado `outcome` (RF-FLW-2).

        Condição exata vence; não achou, cai na aresta `default`; não há
        `default`, devolve None e o ramo termina ali. Uma regra para todos os
        tipos de nó - é o que dispensa um `switch` no motor.
        """
        fallback = None
        for edge in self.outgoing(node_id):
            if edge.condition == outcome:
                return edge.to_id
            if edge.condition == EDGE_DEFAULT:
                fallback = edge.to_id
        return fallback

    def reachable_from(self, start_id: str) -> set[str]:
        seen: set[str] = set()
        stack = [start_id]
        while stack:
            current = stack.pop()
            if current in seen or current not in self._nodes:
                continue
            seen.add(current)
            stack.extend(e.to_id for e in self.outgoing(current))
        return seen


# ---- saídas que cada tipo de nó DEVE ter ----


def required_conditions(node: Node) -> set[str]:
    """
    As saídas sem as quais o nó deixa o paciente pendurado.

    Botão e item de lista geram uma por opção: um botão que não leva a lugar
    nenhum é pior do que não existir, porque o paciente clica e o silêncio
    parece defeito da clínica.
    """
    if node.is_terminal:
        return set()
    if node.type == FlowNodeType.CONDITION:
        return {EDGE_TRUE, EDGE_FALSE}
    if node.type == FlowNodeType.SEND_BUTTONS:
        return {
            f"{EDGE_BUTTON_PREFIX}{b.get('id')}"
            for b in node.config.get("buttons") or []
            if b.get("id")
        }
    if node.type == FlowNodeType.SEND_LIST:
        return {
            f"{EDGE_ROW_PREFIX}{r.get('id')}" for r in node.config.get("rows") or [] if r.get("id")
        }
    return {EDGE_DEFAULT}


# ---- validação (RF-FLW-4) ----


def _tem_fonte(config) -> bool:
    """
    A variável está de fato configurada?

    ⚠️ `bool(config)` não serve: `{}` é falso mas `{"source": ""}` é
    verdadeiro, e um mapa com a chave presente e a fonte em branco passaria
    pela publicação para morrer no envio.
    """
    if not isinstance(config, dict):
        return False
    fonte = (config.get("source") or "").strip()
    if not fonte:
        return False
    # Texto fixo e variável de fluxo guardam o conteúdo em `value`: sem ele a
    # fonte existe e não resolve nada.
    if fonte in ("fixed", "flow_var"):
        return bool((config.get("value") or "").strip())
    return True


def _problemas_do_template(cfg: dict, onde: str, clinic) -> list[str]:
    """
    O nó de template só publica com o template escolhido E todas as variáveis
    mapeadas (RF-FLW-24).

    ⚠️ Aqui é o lugar certo para este erro. O nó existe para falar FORA da
    janela de 24h, e template com parâmetro faltando é recusado pela Meta na
    hora do envio - ou seja, com o paciente do outro lado esperando e o fluxo
    morrendo no meio. Barrar na publicação joga o erro para quem monta, que é
    quem pode consertar.

    Sem `clinic` a checagem cai para o que dá sem consultar os aprovados:
    ainda pega o nó sem template e o mapa com buraco na numeração.
    """
    from apps.inbox.models import WhatsAppTemplate

    nome = (cfg.get("template_name") or "").strip()
    if not nome:
        return [f"{onde} não tem template escolhido."]

    mapa = cfg.get("variables") or {}
    problems: list[str] = []

    if clinic is None:
        # Buraco na numeração desloca TUDO: a Meta casa por posição, e um
        # `{{2}}` faltando faz o valor do `{{3}}` chegar no lugar dele.
        numeros = sorted(int(k) for k in mapa if str(k).isdigit())
        if numeros and numeros != list(range(1, len(numeros) + 1)):
            problems.append(f"{onde} tem um buraco na numeração das variáveis.")
        return problems

    from apps.inbox.template_vars import rotulo_da_variavel, variaveis_do_template

    template = WhatsAppTemplate.objects.filter(clinic=clinic, name=nome).first()
    if template is None:
        return [f'{onde} usa o template "{nome}", que não está aprovado nesta conta.']

    pedidas = variaveis_do_template(template)
    faltando = [chave for chave in pedidas if not _tem_fonte(mapa.get(chave))]
    if faltando:
        quais = ", ".join(rotulo_da_variavel(template, c) for c in faltando)
        problems.append(
            f'{onde} usa o template "{nome}", que pede {len(pedidas)} '
            f"variáveis, e {quais} não foram preenchidas."
        )
    return problems


def _check_config(node: Node, clinic=None) -> list[str]:
    """
    O mínimo para o nó conseguir executar. Não é validação de formulário: é a
    diferença entre o fluxo rodar e morrer no meio de uma conversa real.
    """
    cfg = node.config
    problems: list[str] = []
    onde = f'O nó "{node.label or node.id}"'

    if node.type == FlowNodeType.SEND_MESSAGE and not (cfg.get("text") or "").strip():
        problems.append(f"{onde} não tem mensagem para enviar.")

    elif node.type == FlowNodeType.SEND_BUTTONS:
        botoes = cfg.get("buttons") or []
        if not (cfg.get("text") or "").strip():
            problems.append(f"{onde} não tem o texto que vai acima dos botões.")
        if not botoes:
            problems.append(f"{onde} não tem nenhum botão.")
        if len(botoes) > MAX_BUTTONS:
            problems.append(
                f"{onde} tem {len(botoes)} botões e o WhatsApp aceita no máximo {MAX_BUTTONS}."
            )
        if any(not (b.get("title") or "").strip() for b in botoes):
            problems.append(f"{onde} tem botão sem título.")

    elif node.type == FlowNodeType.SEND_LIST:
        linhas = cfg.get("rows") or []
        if not (cfg.get("text") or "").strip():
            problems.append(f"{onde} não tem o texto da lista.")
        if not linhas:
            problems.append(f"{onde} não tem nenhum item.")
        if len(linhas) > MAX_LIST_ROWS:
            problems.append(
                f"{onde} tem {len(linhas)} itens e o WhatsApp aceita no máximo {MAX_LIST_ROWS}."
            )

    elif node.type == FlowNodeType.SEND_MEDIA and not (cfg.get("media_url") or "").strip():
        problems.append(f"{onde} não tem o endereço da mídia.")

    elif node.type == FlowNodeType.SEND_TEMPLATE:
        problems.extend(_problemas_do_template(cfg, onde, clinic))

    elif node.type == FlowNodeType.COLLECT_INPUT:
        if not (cfg.get("prompt_text") or "").strip():
            problems.append(f"{onde} não tem a pergunta.")
        if not (cfg.get("var_key") or "").strip():
            problems.append(f"{onde} não diz em que variável guardar a resposta.")

    elif node.type == FlowNodeType.CONDITION:
        subject = cfg.get("subject")
        operator = cfg.get("operator")
        if subject not in ConditionSubject.values:
            problems.append(f"{onde} não diz o que comparar.")
        if subject == ConditionSubject.VAR and not (cfg.get("subject_key") or "").strip():
            problems.append(f"{onde} não diz qual variável comparar.")
        if operator not in ConditionOperator.values:
            problems.append(f"{onde} não tem operador de comparação.")
        elif operator in OPERATORS_WITH_VALUE and not (cfg.get("value") or "").strip():
            problems.append(f"{onde} não tem valor para comparar.")

    elif node.type == FlowNodeType.SET_LABEL and not cfg.get("label_id"):
        problems.append(f"{onde} não tem etiqueta escolhida.")

    elif node.type == FlowNodeType.WAIT and not (cfg.get("amount") or 0) > 0:
        problems.append(f"{onde} não tem tempo de espera.")

    return problems


def _has_cycle_without_wait(graph: FlowGraph) -> bool:
    """
    Laço que gira sozinho.

    O truque: um nó que espera o paciente QUEBRA o laço, porque a execução
    para ali até alguém responder. Então some com as arestas que saem desses
    nós e procure ciclo no que sobrou - se ainda houver, o motor giraria
    disparando mensagem em rajada. É o defeito mais caro que um fluxo pode
    ter, porque o WhatsApp bloqueia o número da clínica por spam.
    """
    unvisited, in_stack, done = 0, 1, 2
    state: dict[str, int] = {node.id: unvisited for node in graph.nodes}

    def walk(node_id: str) -> bool:
        state[node_id] = in_stack
        node = graph.node(node_id)
        if node and not node.awaits_contact:
            for edge in graph.outgoing(node_id):
                target = edge.to_id
                if state.get(target) == in_stack:
                    return True
                if state.get(target) == unvisited and walk(target):
                    return True
        state[node_id] = done
        return False

    return any(state[node_id] == unvisited and walk(node_id) for node_id in list(state))


def validate_graph(raw: dict, clinic=None) -> list[str]:
    """
    Os problemas que impedem ATIVAR (RF-FLW-4). Lista vazia = pode ativar.

    Rascunho salva quebrado de propósito: montar um fluxo é trabalho de
    várias sessões. Esta função é a porta de ativar, e devolve frases que o
    gestor entende - ele é quem vai consertar, e "nó órfão" não diz nada a
    quem não desenha grafo.

    `clinic` é opcional e só serve ao nó de template: com ela dá para conferir
    o mapa de variáveis contra o template APROVADO e barrar antes de publicar.
    Sem ela, a checagem cai para o que dá sem consultar a Meta.
    """
    graph = FlowGraph(raw)
    problems: list[str] = []

    if not graph.nodes:
        return ["O fluxo está vazio."]

    # 1. entrada
    entry = graph.node(graph.entry_node)
    if not entry:
        problems.append("O fluxo não tem um nó de início.")
    elif entry.type != FlowNodeType.START:
        problems.append("O fluxo precisa começar por um nó de início.")

    # 2. arestas que apontam para o nada
    for node in graph.nodes:
        for edge in graph.outgoing(node.id):
            if not graph.node(edge.to_id):
                nome = node.label or node.id
                problems.append(f'A ligação que sai de "{nome}" não chega a lugar nenhum.')

    # 3. saídas obrigatórias e config
    for node in graph.nodes:
        faltando = required_conditions(node) - graph.conditions_of(node.id)
        if faltando:
            problems.append(
                f'O nó "{node.label or node.id}" tem {len(faltando)} saída(s) sem ligação.'
            )
        if node.is_terminal and graph.outgoing(node.id):
            problems.append(f'O nó "{node.label or node.id}" encerra o fluxo e não pode ter saída.')
        problems.extend(_check_config(node, clinic))

    # 4. nós que ninguém alcança
    if entry:
        alcancaveis = graph.reachable_from(graph.entry_node)
        orfaos = [n for n in graph.nodes if n.id not in alcancaveis]
        if orfaos:
            nomes = ", ".join(f'"{n.label or n.id}"' for n in orfaos[:3])
            resto = f" e mais {len(orfaos) - 3}" if len(orfaos) > 3 else ""
            problems.append(f"O fluxo nunca chega em: {nomes}{resto}.")

    # 5. laço infinito
    if _has_cycle_without_wait(graph):
        problems.append(
            "O fluxo tem um caminho que volta para trás sem esperar o paciente responder, "
            "e ficaria enviando mensagens sem parar."
        )

    return problems
