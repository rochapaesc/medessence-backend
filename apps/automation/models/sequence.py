from datetime import time

from django.db.models import (
    CASCADE,
    RESTRICT,
    SET_NULL,
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKey,
    PositiveSmallIntegerField,
    Q,
    SmallIntegerField,
    TimeField,
    UniqueConstraint,
)

from apps.automation.choices import (
    EnrollmentSource,
    SequenceDispatchStatus,
    SequenceEnrollmentStatus,
)
from apps.core.models import BaseModel, TenantScopedModel

# Quanto o disparo espera antes de tentar de novo quando encontra a caneta
# ocupada (RF-SEQ-5). Cinco minutos, e não um: atendente que assumiu a conversa
# fica com ela por dezenas de minutos, e insistir a cada varredura gastaria
# consulta ao banco sem adiantar nada. É também o intervalo que a trava usa
# como reserva, então ele é o tempo máximo que um disparo fica parado depois de
# um worker morrer no meio.
DISPATCH_RETRY_MINUTES = 5

# Validade padrão de um passo (RF-SEQ-5.1). Um dia: o lembrete que não
# conseguiu sair em 24h chegaria descolado do motivo que o gerou.
DEFAULT_EXPIRE_HOURS = 24

# Janela padrão do "agendou" (RF-SEQ-11.2). Uma semana pega quem marca no
# impulso e quem marca no dia seguinte, sem creditar à trilha um agendamento
# que veio de outra coisa duas semanas depois.
DEFAULT_CONVERSION_DAYS = 7


class Sequence(TenantScopedModel):
    """
    Uma trilha de disparos no tempo (§4.4).

    A sequência é o CALENDÁRIO; quem conversa é o fluxo de cada passo. Ela não
    manda mensagem, não segura a caneta da conversa e não trata resposta - se o
    passo precisa conversar, o fluxo dele conversa (RF-SEQ-1).
    """

    name = CharField(verbose_name="Nome", max_length=120)
    is_active = BooleanField(
        verbose_name="Ativa",
        default=False,
        help_text=(
            "Nasce DESLIGADA (RF-SEQ-12): ligar é gesto do gestor. Desligada, "
            "os disparos ficam segurados e o calendário retoma ao religar."
        ),
    )
    is_marketing = BooleanField(
        verbose_name="É de marketing",
        default=True,
        help_text=(
            "Ligado, respeita o opt-out do contato (RF-SEQ-8.2). Desligue em "
            "sequência operacional - quem pediu para parar promoção não pediu "
            "para parar de confirmar consulta."
        ),
    )
    enroll_on_appointment = BooleanField(
        verbose_name="Inscrever quando marcar consulta",
        default=False,
        help_text=(
            "Porta automática (RF-SEQ-3.4): consulta futura criada aqui ou "
            "espelhada do prontuário inscreve, ancorada na data dela."
        ),
    )
    conversion_days = PositiveSmallIntegerField(
        verbose_name="Janela do agendou (dias)",
        default=DEFAULT_CONVERSION_DAYS,
        help_text=(
            "Quantos dias depois de um disparo uma consulta nova conta como "
            "resultado dele (RF-SEQ-11.2). É por sequência porque confirmar "
            "véspera e resgatar inativo têm ritmos diferentes."
        ),
    )

    class Meta:
        verbose_name = "Sequência"
        verbose_name_plural = "Sequências"
        ordering = ["name"]
        constraints = [
            UniqueConstraint(
                fields=["clinic", "name"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_sequence_name",
            ),
        ]

    def __str__(self):
        return self.name


class SequenceStep(BaseModel):
    """
    Um passo: quando disparar e QUAL FLUXO disparar (RF-SEQ-1/2).

    ⚠️ O prazo conta da ÂNCORA da inscrição, nunca do passo anterior. É desvio
    deliberado do BotConversa (que conta "o tempo entre os fluxos"), e paga
    três coisas: o D-1 existe (deslocamento negativo é impossível em cadeia),
    remarcar recalcula tudo de uma vez sem arrastar erro acumulado, e o painel
    mostra o calendário inteiro no ato da inscrição.
    """

    sequence = ForeignKey(
        Sequence,
        verbose_name="Sequência",
        on_delete=CASCADE,
        related_name="steps",
    )
    order = PositiveSmallIntegerField(verbose_name="Ordem")
    name = CharField(verbose_name="Nome", max_length=120, blank=True)
    offset_days = SmallIntegerField(
        verbose_name="Dias da âncora",
        help_text="Negativo é antes: -1 é a véspera da consulta. Zero é o próprio dia.",
    )
    send_time = TimeField(
        verbose_name="Horário",
        default=time(8, 0),
        help_text="Hora LOCAL da clínica, não do servidor.",
    )
    flow = ForeignKey(
        "automation.Flow",
        verbose_name="Fluxo",
        on_delete=RESTRICT,
        related_name="sequence_steps",
        help_text="O fluxo que este passo dispara. Precisa estar publicado na hora do disparo.",
    )
    expire_hours = PositiveSmallIntegerField(
        verbose_name="Validade (horas)",
        default=DEFAULT_EXPIRE_HOURS,
        help_text=(
            "Quanto tempo insistir quando o disparo encontra a conversa "
            "ocupada. Vencido, o passo é PULADO com o motivo gravado, e o "
            "calendário segue - o passo seguinte não desliza (RF-SEQ-5.1)."
        ),
    )
    is_active = BooleanField(verbose_name="Ativo", default=True)

    class Meta:
        verbose_name = "Passo da sequência"
        verbose_name_plural = "Passos da sequência"
        ordering = ["order"]
        constraints = [
            UniqueConstraint(
                fields=["sequence", "order"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_step_order",
            ),
        ]

    def __str__(self):
        return self.name or f"Passo {self.order}"


class SequenceEnrollment(TenantScopedModel):
    """
    Um contato dentro de uma sequência (RF-SEQ-3/4).

    A ÂNCORA é o que unifica as quatro portas: nas três de gente ela é o
    instante da inscrição, e na automática é a data da consulta - e aí o D-1
    vira só um deslocamento negativo, sem modelo separado.
    """

    sequence = ForeignKey(
        Sequence,
        verbose_name="Sequência",
        on_delete=CASCADE,
        related_name="enrollments",
    )
    contact = ForeignKey(
        "patients.Contact",
        verbose_name="Contato",
        on_delete=CASCADE,
        related_name="sequence_enrollments",
        help_text="Quem RECEBE. A inscrição é do número, não do paciente (RF-SEQ-3.6).",
    )
    patient = ForeignKey(
        "patients.Patient",
        verbose_name="Paciente",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="sequence_enrollments",
    )
    appointment = ForeignKey(
        "scheduling.Appointment",
        verbose_name="Consulta",
        null=True,
        blank=True,
        on_delete=CASCADE,
        related_name="sequence_enrollments",
        help_text="Só na porta automática (RF-SEQ-3.4). É ela que ancora os deslocamentos.",
    )
    anchor_at = DateTimeField(
        verbose_name="Âncora",
        help_text="O instante zero dos deslocamentos dos passos.",
    )
    source = CharField(
        verbose_name="Porta de entrada",
        max_length=20,
        choices=EnrollmentSource.choices,
    )
    status = CharField(
        verbose_name="Situação",
        max_length=20,
        choices=SequenceEnrollmentStatus.choices,
        default=SequenceEnrollmentStatus.ACTIVE,
    )
    end_reason = CharField(verbose_name="Motivo do fim", max_length=30, blank=True)
    current_step = ForeignKey(
        SequenceStep,
        verbose_name="Passo atual",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="+",
    )
    next_dispatch_at = DateTimeField(
        verbose_name="Próximo disparo",
        null=True,
        blank=True,
        db_index=True,
        help_text="Base da varredura. Nulo quando não há mais passo pela frente.",
    )
    held_since = DateTimeField(
        verbose_name="Segurando desde",
        null=True,
        blank=True,
        help_text=(
            "Quando o disparo começou a ser adiado (RF-SEQ-11). Nulo quando "
            "não há nada segurando."
        ),
    )
    hold_reason = CharField(
        verbose_name="Motivo de estar segurando",
        max_length=40,
        blank=True,
        help_text=(
            "Por que o disparo não sai AGORA. Sem isto o painel mostraria a "
            "hora passada e nenhuma explicação, que é o mesmo mistério que os "
            "motivos de pulo existem para acabar."
        ),
    )

    class Meta:
        verbose_name = "Inscrição em sequência"
        verbose_name_plural = "Inscrições em sequência"
        ordering = ["-created_at"]
        constraints = [
            # RF-SEQ-4, mesmo padrão do RF-FLW-6: a trava é do BANCO. Duas
            # portas podem inscrever o mesmo contato no mesmo instante (o nó de
            # um fluxo e o lote da recepção), e um `if not exists` em Python
            # perde essa corrida.
            UniqueConstraint(
                fields=["sequence", "contact"],
                condition=Q(
                    status=SequenceEnrollmentStatus.ACTIVE,
                    appointment__isnull=True,
                    deleted_at__isnull=True,
                ),
                name="uniq_enrollment_ativo_por_contato",
            ),
            # As ancoradas em consulta ficam de fora da trava acima de
            # propósito: duas consultas futuras do mesmo paciente são duas
            # jornadas legítimas, cada uma com a própria âncora.
            UniqueConstraint(
                fields=["sequence", "appointment"],
                condition=Q(
                    status=SequenceEnrollmentStatus.ACTIVE,
                    deleted_at__isnull=True,
                ),
                name="uniq_enrollment_ativo_por_consulta",
            ),
        ]

    def __str__(self):
        return f"{self.contact_id} em {self.sequence_id}"


class SequenceDispatch(BaseModel):
    """
    O histórico: uma linha por passo RESOLVIDO, disparado ou pulado.

    É onde mora a resposta a "por que o paciente não recebeu?" (RF-SEQ-5.1) e
    de onde o painel tira a fila por etapa. Também é a rede de idempotência: a
    unicidade por (inscrição, passo) impede que um worker morto no meio faça o
    mesmo passo disparar duas vezes.
    """

    enrollment = ForeignKey(
        SequenceEnrollment,
        verbose_name="Inscrição",
        on_delete=CASCADE,
        related_name="dispatches",
    )
    step = ForeignKey(
        SequenceStep,
        verbose_name="Passo",
        on_delete=RESTRICT,
        related_name="dispatches",
    )
    scheduled_for = DateTimeField(verbose_name="Previsto para")
    resolved_at = DateTimeField(verbose_name="Resolvido em", null=True, blank=True)
    flow_run = ForeignKey(
        "automation.FlowRun",
        verbose_name="Execução gerada",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="+",
    )
    status = CharField(
        verbose_name="Situação",
        max_length=20,
        choices=SequenceDispatchStatus.choices,
    )
    skip_reason = CharField(verbose_name="Motivo do pulo", max_length=30, blank=True)

    class Meta:
        verbose_name = "Disparo da sequência"
        verbose_name_plural = "Disparos da sequência"
        ordering = ["scheduled_for"]
        constraints = [
            UniqueConstraint(
                fields=["enrollment", "step"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_dispatch_por_passo",
            ),
        ]

    def __str__(self):
        return f"{self.enrollment_id} · passo {self.step_id} · {self.status}"
