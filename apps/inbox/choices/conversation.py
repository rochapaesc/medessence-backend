from django.db.models import TextChoices


class ConversationStatus(TextChoices):
    """
    Ciclo de vida do atendimento (§4.3.1, RF-ATD-1).

    Substitui o `needs_agent` booleano da F2: um bit não distingue "ninguém
    pegou ainda" de "adiada" nem de "encerrada", e sem esses estados a lista
    só cresce - a conversa nasce e nunca termina.

    RESOLVED não arquiva (RF-ATD-2): mensagem do paciente reabre a MESMA
    conversa. O que começa e termina é o ATENDIMENTO, não a conversa - por
    isso "resolvidos no mês" conta transições, não linhas.
    """

    WAITING = "waiting", "Aguardando"
    OPEN = "open", "Aberta"
    SNOOZED = "snoozed", "Adiada"
    RESOLVED = "resolved", "Resolvida"


# Status que somem da fila de trabalho - mensagem nova os reabre (RF-ATD-2).
DORMANT_STATUSES = (ConversationStatus.RESOLVED, ConversationStatus.SNOOZED)


class AttendedBy(TextChoices):
    """
    Quem tem a caneta (RF-ATD-12). ORTOGONAL ao status: conversa com a IA
    continua `OPEN` - está sendo atendida, só que por ela. Misturar as duas
    coisas num enum só faria toda fila e filtro herdarem a confusão entre
    "em que ponto está" e "quem está mexendo".
    """

    NONE = "none", "Ninguém"
    BOT = "bot", "IA"
    AGENT = "agent", "Atendente"


class ConversationPriority(TextChoices):
    """
    Urgência da conversa (RF-ATD-8). ORDENA a fila, não a filtra: um chip a
    mais numa barra que já tem cinco cobraria um clique para ver o que já
    deveria estar na frente - e esconderia o resto da fila enquanto ligado.

    NORMAL é "" para o campo nascer vazio sem backfill: a conversa que ninguém
    priorizou não é uma decisão, é a ausência dela.
    """

    NORMAL = "", "Normal"
    HIGH = "high", "Alta"
    URGENT = "urgent", "Urgente"


# Havia aqui um PRIORITY_RANK que ordenava a fila por urgência antes da
# recência. Saiu em 28/07/2026: enterrava a mensagem recém-chegada embaixo de
# uma urgente de ontem. Prioridade agora é tarja, selo e filtro — não ordem.


class ActivityType(TextChoices):
    """
    Evento na linha do tempo (RF-ATD-4). Fica na MESMA tabela de mensagens
    (padrão Chatwoot) para a thread contar a história inteira numa consulta.

    O texto NÃO é montado aqui: o tipo e o `activity_data` vão estruturados e
    quem renderiza é o front - backend que concatena frase engessa tradução e
    formatação.
    """

    ASSIGNED = "assigned", "Assumiu o atendimento"
    TRANSFERRED = "transferred", "Transferiu"
    RESOLVED = "resolved", "Resolveu"
    REOPENED = "reopened", "Reaberta"
    SNOOZED = "snoozed", "Adiou"
    LABEL_ADDED = "label_added", "Marcou etiqueta"
    LABEL_REMOVED = "label_removed", "Removeu etiqueta"
    PRIORITY_CHANGED = "priority_changed", "Mudou a prioridade"
    BOT_STARTED = "bot_started", "IA assumiu"
    BOT_HANDOFF = "bot_handoff", "IA entregou para humano"
    TAKEN_OVER = "taken_over", "Tomou o atendimento"
