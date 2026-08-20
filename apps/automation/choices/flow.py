from django.db.models import TextChoices


class FlowStatus(TextChoices):
    """
    Estado do fluxo (§4.3.2, RF-FLW-3). Só ACTIVE dispara.

    Rascunho salva quebrado de propósito - montar um fluxo é trabalho de
    várias sessões, e exigir que ele esteja íntegro para salvar obrigaria o
    gestor a terminar de uma vez. A validação (RF-FLW-4) é a porta de ATIVAR.
    """

    DRAFT = "draft", "Rascunho"
    ACTIVE = "active", "Ativo"
    ARCHIVED = "archived", "Arquivado"


class FlowTrigger(TextChoices):
    """
    O que faz o fluxo começar (RF-FLW-5).

    O protótipo do cliente não tinha isto: o nó de início não dizia o que o
    dispara. É a decisão de produto mais pesada do módulo - FIRST_INBOUND
    coloca o robô na frente da recepção em toda conversa nova, e por isso
    quase sempre anda junto com `only_outside_hours` (RF-FLW-5.1).
    """

    FIRST_INBOUND = "first_inbound", "Primeira mensagem do contato"
    # ⚠️ NÃO é sinônimo do de cima, e a diferença aparece no segundo mês de uso
    # (RF-FLW-5.2, 20/08/2026). Aqui a conversa é ÚNICA por contato, então
    # "primeira mensagem" acontece UMA VEZ NA VIDA: quem já falou com a clínica
    # em março nunca mais dispara o fluxo de acolhida. Este dispara a cada
    # ATENDIMENTO novo — conversa que nasce, e conversa que volta depois de
    # encerrada. É o `conversation_created` do Chatwoot, onde cada atendimento
    # é uma conversa nova; como a nossa não é, a reabertura faz esse papel.
    NEW_CONVERSATION = "new_conversation", "Novo atendimento (conversa nova ou reaberta)"
    KEYWORD = "keyword", "Palavra-chave"
    MANUAL = "manual", "Disparo manual do atendente"


class FlowRunStatus(TextChoices):
    """
    Estado de UMA execução, para UM contato (RF-FLW-7).

    PAUSED_BY_AGENT é o RF-FLW-9 e não é sinônimo de terminada: o humano
    assumiu e o robô NÃO volta sozinho, nem quando o paciente responde de
    novo. Devolver à máquina é ato explícito - é o RF-ATD-17 aplicado aqui.
    """

    ACTIVE = "active", "Em andamento"
    COMPLETED = "completed", "Concluída"
    HANDED_OFF = "handed_off", "Entregue a um humano"
    TIMED_OUT = "timed_out", "Encerrada por inatividade"
    PAUSED_BY_AGENT = "paused_by_agent", "Pausada porque um atendente assumiu"
    FAILED = "failed", "Falhou"


# Execuções que não avançam mais. Só ACTIVE conta para a trava de "uma
# execução ativa por contato" (RF-FLW-6).
FINISHED_RUN_STATUSES = (
    FlowRunStatus.COMPLETED,
    FlowRunStatus.HANDED_OFF,
    FlowRunStatus.TIMED_OUT,
    FlowRunStatus.PAUSED_BY_AGENT,
    FlowRunStatus.FAILED,
)


class FlowNodeType(TextChoices):
    """
    Catálogo de nós da v1 (RF-FLW-13). Doze, e NENHUM com IA.

    O protótipo trazia dezesseis. Ficaram de fora: LLM_AGENT e
    TRANSCRIBE_AUDIO (P14 - conversa e áudio de paciente para operador no
    exterior), HTTP_REQUEST (P15 - dado de paciente para qualquer URL que o
    autor digitar) e MESSAGE_ROUTER, que só existia para desviar áudio à
    transcrição e sem ela não tem para onde desviar.

    ⚠️ SET_LABEL, não "set_tag": a `patients.Tag` SINCRONIZA COM A vSAÚDE, e
    um fluxo marcando "lead-quente" tentaria escrever no prontuário. Aqui é
    `inbox.ConversationLabel`, que é local (RF-FLW-13.1).
    """

    START = "start", "Início"
    SEND_MESSAGE = "send_message", "Enviar mensagem"
    SEND_BUTTONS = "send_buttons", "Enviar botões"
    SEND_LIST = "send_list", "Enviar lista"
    SEND_MEDIA = "send_media", "Enviar mídia"
    SEND_TEMPLATE = "send_template", "Enviar template"
    COLLECT_INPUT = "collect_input", "Coletar resposta"
    CONDITION = "condition", "Se / senão"
    SET_LABEL = "set_label", "Marcar etiqueta"
    WAIT = "wait", "Aguardar"
    HANDOFF = "handoff", "Transferir para humano"
    END = "end", "Fim"
    # F3 (RF-SEQ-3.1): ação instantânea, como marcar etiqueta - não falam com o
    # paciente e não esperam resposta. É por aqui que o robô coloca alguém numa
    # trilha depois de entender o que a pessoa quer.
    ENROLL_SEQUENCE = "enroll_sequence", "Inscrever na sequência"
    UNENROLL_SEQUENCE = "unenroll_sequence", "Remover da sequência"


# Nós que encerram o ramo: não têm saída, e aresta partindo deles é defeito
# de montagem que o validador recusa (RF-FLW-4).
TERMINAL_NODE_TYPES = (FlowNodeType.HANDOFF, FlowNodeType.END)

# Nós que PARAM e esperam o paciente falar. Um ciclo que só passa por nós
# fora desta lista gira sozinho e manda mensagem em rajada - é exatamente o
# que o validador procura (RF-FLW-4).
AWAITING_NODE_TYPES = (
    FlowNodeType.SEND_BUTTONS,
    FlowNodeType.SEND_LIST,
    FlowNodeType.COLLECT_INPUT,
    FlowNodeType.WAIT,
)


class FlowRunEventType(TextChoices):
    """
    Linha do tempo da execução (RF-FLW-12).

    Existe para o gestor responder "em que pergunta as pessoas desistem",
    que é a única métrica que faz alguém mexer no fluxo depois de montado.
    """

    ENTERED = "entered", "Entrou no nó"
    SENT = "sent", "Enviou mensagem"
    REPLIED = "replied", "Paciente respondeu"
    REPROMPT = "reprompt", "Repetiu a pergunta"
    HANDOFF = "handoff", "Entregou a um humano"
    ENDED = "ended", "Terminou"
    # Os três abaixo entraram com o modo de teste (RF-FLW-25.3), mas valem em
    # produção também: são o "o que o fluxo fez" que a linha do tempo não
    # contava (guardou o quê, marcou o quê, mexeu em qual sequência).
    VAR_SAVED = "var_saved", "Guardou uma resposta"
    LABEL_APPLIED = "label_applied", "Marcou etiqueta"
    SEQUENCE_APPLIED = "sequence_applied", "Mexeu numa sequência"


# ---- condições de aresta (RF-FLW-2) ----
# A aresta carrega a saída em TEXTO, não em enum: os nós de botão e de lista
# produzem uma condição por opção (`button:<id>`), e o conjunto só é
# conhecido quando o fluxo é montado. Estas são as fixas.
EDGE_DEFAULT = "default"
EDGE_TRUE = "true"
EDGE_FALSE = "false"
EDGE_BUTTON_PREFIX = "button:"
EDGE_ROW_PREFIX = "row:"


class ConditionSubject(TextChoices):
    """
    O que o nó "Se / senão" pergunta.

    `in_hours` do whatomate é condição de ARESTA lá; aqui virou um assunto de
    CONDITION (decisão de 31/07/2026, ao escrever o validador). Assim existe
    uma regra só - todo CONDITION tem exatamente as saídas `true` e `false` -
    em vez de uma condição de aresta especial que só um caso usa.
    """

    VAR = "var", "Uma variável coletada"
    BUSINESS_HOURS = "business_hours", "A clínica está aberta"


class ConditionOperator(TextChoices):
    EQUALS = "equals", "é igual a"
    CONTAINS = "contains", "contém"
    PRESENT = "present", "está preenchido"
    ABSENT = "absent", "está vazio"


# Operadores que comparam com um valor digitado; os outros só olham se a
# variável existe.
OPERATORS_WITH_VALUE = (ConditionOperator.EQUALS, ConditionOperator.CONTAINS)

# Tetos da Meta (§7). Não são preferência nossa: passar disso faz a mensagem
# ser RECUSADA no envio, e o fluxo já estaria publicado e rodando.
MAX_BUTTONS = 3
MAX_LIST_ROWS = 10
