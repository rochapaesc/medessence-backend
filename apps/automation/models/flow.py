from django.db.models import (
    CASCADE,
    RESTRICT,
    SET_NULL,
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKey,
    JSONField,
    PositiveIntegerField,
    PositiveSmallIntegerField,
    Q,
    UniqueConstraint,
)
from django.utils import timezone

from apps.automation.choices import FlowRunStatus, FlowStatus, FlowTrigger
from apps.core.models import BaseModel, TenantScopedModel

# Fala de despedida do handoff automático (RF-FLW-11.1). Dois textos porque
# são dois motivos: "não entendi" e "você sumiu" pedem frases diferentes, e uma
# só mentiria em um dos casos.
GOODBYE_REPROMPT = "Desculpe, não consegui entender. Vou chamar alguém da recepção para te ajudar."
GOODBYE_TIMEOUT = (
    "Como não tive retorno, vou deixar a sua conversa com a recepção. É só escrever quando puder."
)

# Teto de falas do robô numa execução (RF-FLW-23.1). O fluxo real da clínica
# tem 25 nós e gasta 8 a 10 falas numa conversa completa, uns 20 com todos os
# reprompts. Trinta é folga e ainda corta um laço cedo: o risco do ping-pong
# com outro robô não é o custo por mensagem (a Meta cobra por conversa de 24h)
# e sim a Meta bloquear o número da clínica por spam.
MAX_BOT_MESSAGES = 30


def default_fallback():
    """
    Política de fallback de um fluxo novo (RF-FLW-11).

    Os números respondem a perguntas diferentes: quantas vezes repetir quando
    a resposta não casa com saída nenhuma, quanto tempo esperar antes de
    desistir do silêncio, e o que fazer no fim dos dois. `handoff` é o único
    fim aceitável - abandonar a conversa em silêncio é o comportamento que o
    Inbox existe para impedir.

    ⚠️ **20 horas, e não 24** (RF-FLW-11.3). Os dois relógios são diferentes:
    a janela da Meta conta 24h desde a última fala do PACIENTE e este timeout
    conta desde o último avanço da EXECUÇÃO, que é sempre igual ou posterior.
    Com 24 a despedida caía fora da janela e só sairia como template pago.
    """
    return {
        "max_reprompts": 2,
        "on_timeout_hours": 20,
        "on_exhaust": "handoff",
        "goodbye_reprompt": GOODBYE_REPROMPT,
        "goodbye_timeout": GOODBYE_TIMEOUT,
        "max_bot_messages": MAX_BOT_MESSAGES,
    }


def empty_graph():
    return {"nodes": [], "edges": [], "entry_node": ""}


class Flow(TenantScopedModel):
    """
    Fluxo de atendimento (§4.3.2): o robô que atende no WhatsApp.

    O grafo NÃO mora aqui - mora em `FlowVersion.graph` (RF-FLW-1). Isto
    aqui é a identidade e a política: como começa, quando pode começar e o
    que fazer quando o paciente cala. Editar o desenho cria versão; mudar a
    política, não.
    """

    name = CharField(verbose_name="Nome", max_length=120)
    status = CharField(
        verbose_name="Situação",
        max_length=20,
        choices=FlowStatus.choices,
        default=FlowStatus.DRAFT,
    )
    trigger = CharField(
        verbose_name="Gatilho",
        max_length=20,
        choices=FlowTrigger.choices,
        default=FlowTrigger.FIRST_INBOUND,
    )
    trigger_config = JSONField(
        verbose_name="Configuração do gatilho",
        default=dict,
        blank=True,
        help_text='Para palavra-chave: {"keywords": ["orçamento"], "match": "contains"}.',
    )
    only_outside_hours = BooleanField(
        verbose_name="Só fora do horário de funcionamento",
        default=False,
        help_text=(
            "Ligado, o fluxo só dispara com a clínica fechada - dentro do "
            "expediente a conversa vai direto para a recepção. É o modo que "
            "faz sentido para uma recepção pequena (RF-FLW-5.1)."
        ),
    )
    priority = PositiveSmallIntegerField(
        verbose_name="Prioridade",
        default=10,
        help_text=(
            "Desempate quando mais de um fluxo casa com a mesma mensagem: "
            "MENOR vence. Empatou, vence o ativado por último (RF-FLW-5.2)."
        ),
    )
    fallback = JSONField(verbose_name="Política de fallback", default=default_fallback)
    current_version = ForeignKey(
        "automation.FlowVersion",
        verbose_name="Versão publicada",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="+",
        help_text="A versão que as execuções NOVAS usam. Nula enquanto nunca foi ativado.",
    )
    activated_at = DateTimeField(verbose_name="Ativado em", null=True, blank=True)

    class Meta:
        verbose_name = "Fluxo de atendimento"
        verbose_name_plural = "Fluxos de atendimento"
        ordering = ["priority", "name"]
        constraints = [
            UniqueConstraint(
                fields=["clinic", "name"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_flow_name",
            ),
        ]

    def __str__(self):
        return self.name


class FlowVersion(BaseModel):
    """
    Uma versão do desenho (RF-FLW-1). O grafo INTEIRO num JSON.

    Não é uma linha por nó (que é como o wacrm faz) por dois motivos: mover
    um nó no canvas viraria UPDATE em tabela, e principalmente porque a
    execução precisa se prender à versão em que começou - editar o fluxo às
    14h não pode quebrar o paciente que parou no nó 5 às 11h, e com nós
    soltos em tabela o nó em que ele está pode ter deixado de existir
    (RF-FLW-1.1). O whatomate já esteve na outra forma e migrou para esta.

    Sem FK de clínica: o escopo vem do `flow`.
    """

    flow = ForeignKey(
        Flow,
        verbose_name="Fluxo",
        on_delete=CASCADE,
        related_name="versions",
    )
    number = PositiveIntegerField(verbose_name="Número")
    graph = JSONField(
        verbose_name="Grafo",
        default=empty_graph,
        help_text='{"nodes": [...], "edges": [...], "entry_node": "n1"}',
    )
    published_at = DateTimeField(
        verbose_name="Publicada em",
        null=True,
        blank=True,
        help_text="Nula = rascunho. Publicar é o que a torna elegível a virar a versão corrente.",
    )

    class Meta:
        verbose_name = "Versão do fluxo"
        verbose_name_plural = "Versões do fluxo"
        ordering = ["-number"]
        constraints = [
            UniqueConstraint(
                fields=["flow", "number"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_flow_version_number",
            ),
        ]

    def __str__(self):
        return f"{self.flow_id} v{self.number}"


class FlowRun(TenantScopedModel):
    """
    Uma execução, para um contato (RF-FLW-7).

    É a peça que o protótipo do cliente não tinha e sem a qual nada atende:
    ela é a memória de que aquele paciente parou no nó do menu às 22h de
    sexta e ainda está esperando resposta no sábado de manhã.
    """

    flow = ForeignKey(
        Flow,
        verbose_name="Fluxo",
        on_delete=CASCADE,
        related_name="runs",
    )
    version = ForeignKey(
        FlowVersion,
        verbose_name="Versão",
        on_delete=RESTRICT,
        related_name="runs",
        help_text="A versão em que ESTA execução começou e na qual ela termina (RF-FLW-1.1).",
    )
    contact = ForeignKey(
        "patients.Contact",
        verbose_name="Contato",
        null=True,
        on_delete=SET_NULL,
        related_name="flow_runs",
    )
    conversation = ForeignKey(
        "inbox.Conversation",
        verbose_name="Conversa",
        null=True,
        on_delete=SET_NULL,
        related_name="flow_runs",
    )
    status = CharField(
        verbose_name="Situação",
        max_length=20,
        choices=FlowRunStatus.choices,
        default=FlowRunStatus.ACTIVE,
    )
    current_node = CharField(verbose_name="Nó atual", max_length=100, blank=True)
    reprompt_count = PositiveSmallIntegerField(verbose_name="Repetições", default=0)
    vars = JSONField(
        verbose_name="Variáveis",
        default=dict,
        blank=True,
        help_text="O que os nós de coleta capturaram. Escopo da execução: morre com ela.",
    )
    last_advanced_at = DateTimeField(
        verbose_name="Último avanço",
        default=timezone.now,
        db_index=True,
        help_text="Base da varredura de inatividade (RF-FLW-11).",
    )
    wake_at = DateTimeField(
        verbose_name="Acordar em",
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "Preenchido pelo nó 'Aguardar': quando a varredura deve retomar "
            "sozinha. Nulo = a execução espera o paciente falar, não o relógio."
        ),
    )
    ended_at = DateTimeField(verbose_name="Encerrada em", null=True, blank=True)
    end_reason = CharField(verbose_name="Motivo do fim", max_length=40, blank=True)

    class Meta:
        verbose_name = "Execução de fluxo"
        verbose_name_plural = "Execuções de fluxo"
        ordering = ["-created_at"]
        constraints = [
            # RF-FLW-6. A trava é do BANCO, não da aplicação: duas entregas
            # simultâneas do mesmo webhook são o caso normal, e um
            # `if not exists` no Python perde essa corrida. A segunda
            # inserção estoura IntegrityError e o motor a trata como no-op.
            UniqueConstraint(
                fields=["clinic", "contact"],
                condition=Q(status=FlowRunStatus.ACTIVE, deleted_at__isnull=True),
                name="uniq_run_ativo_por_contato",
            ),
        ]

    def __str__(self):
        return f"{self.flow_id} · {self.contact_id} · {self.status}"


class FlowRunEvent(BaseModel):
    """
    Passo a passo de uma execução (RF-FLW-12).

    Serve a uma pergunta só, e ela é de gestor: em que ponto as pessoas
    desistem. Sem isso o fluxo é montado uma vez e nunca mais melhora.
    """

    run = ForeignKey(
        FlowRun,
        verbose_name="Execução",
        on_delete=CASCADE,
        related_name="events",
    )
    node_key = CharField(verbose_name="Nó", max_length=100, blank=True)
    event_type = CharField(verbose_name="Tipo", max_length=20)
    data = JSONField(verbose_name="Dados", default=dict, blank=True)

    class Meta:
        verbose_name = "Evento de execução"
        verbose_name_plural = "Eventos de execução"
        ordering = ["created_at"]

    def __str__(self):
        return f"{self.run_id} · {self.event_type}"
