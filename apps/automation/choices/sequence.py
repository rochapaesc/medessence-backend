from django.db.models import TextChoices


class SequenceEnrollmentStatus(TextChoices):
    """
    Situação de uma inscrição (RF-SEQ-4/6).

    ⚠️ NÃO existe `PAUSED`, e a ausência é decisão: atendente com a conversa na
    mão SEGURA o próximo disparo (RF-SEQ-5), mas não muda o estado da
    inscrição. Estado que muda a cada atendimento viraria ruído no painel e
    exigiria alguém para despausar - o relógio já resolve sozinho.
    """

    ACTIVE = "active", "Ativa"
    COMPLETED = "completed", "Concluída"
    CANCELED = "canceled", "Cancelada"


class EnrollmentEndReason(TextChoices):
    """Por que a inscrição terminou. Vazio enquanto ela vive."""

    FINISHED = "finished", "Passos concluídos"
    OPTED_OUT = "opted_out", "Pediu para não receber"
    MANUAL = "manual", "Removida à mão"
    FLOW_NODE = "flow_node", "Removida por um fluxo"
    APPOINTMENT_CANCELED = "appointment_canceled", "Consulta cancelada"
    SEQUENCE_RETIRED = "sequence_retired", "Sequência aposentada"
    REPLIED = "replied", "Respondeu"
    SCHEDULED = "scheduled", "Marcou consulta"


class SequenceDispatchStatus(TextChoices):
    """
    O que aconteceu com UM passo (RF-SEQ-5).

    ⚠️ Não existe `PENDING`: a linha só nasce quando o passo se resolve. "Ainda
    vai sair" não é estado gravado, é `next_dispatch_at` no futuro - e gravar
    pendência criaria duas fontes da verdade para a mesma pergunta.
    """

    STARTED = "started", "Fluxo disparado"
    SKIPPED = "skipped", "Pulado"


class DispatchSkipReason(TextChoices):
    """
    Por que um passo não saiu (RF-SEQ-5.1).

    Existe porque pular em silêncio é o defeito das referências: no wacrm a
    pendência estacionada dispara mesmo obsoleta, e no Chatwoot o corte de 3
    dias é mudo. A recepção precisa conseguir responder "por que o paciente
    não recebeu?" sem abrir log.
    """

    EXPIRED = "expired", "Passou da validade"
    NO_WINDOW = "no_window", "Fora da janela e o fluxo não abre com modelo"
    HAS_APPOINTMENT = "has_appointment", "Já tem consulta marcada"
    PATIENT_NO_SHOW = "patient_no_show", "Paciente faltou"
    FLOW_UNPUBLISHED = "flow_unpublished", "Fluxo sem versão publicada"
    NO_CHANNEL = "no_channel", "Clínica sem canal de WhatsApp"
    REORDERED = "reordered", "O prazo do passo mudou depois que a pessoa passou"


class HoldReason(TextChoices):
    """
    Por que um disparo está SEGURANDO agora (RF-SEQ-11).

    Segurar não é pular: nada foi consumido e o relógio volta a tentar. Mas
    "previsto para as 08:00" com as 10:00 no relógio e nenhuma explicação é o
    mesmo mistério que os motivos de pulo existem para acabar.
    """

    BUSY = "busy", "A recepção está com a conversa"
    NO_WINDOW = "no_window", "Fora da janela e o fluxo não abre com modelo"
    SEQUENCE_OFF = "sequence_off", "A sequência está desligada"


class EnrollmentSource(TextChoices):
    """As quatro portas do RF-SEQ-3, para o painel dizer por onde cada um entrou."""

    FLOW_NODE = "flow_node", "Nó de fluxo"
    PATIENT_RECORD = "patient_record", "Ficha do paciente"
    BATCH = "batch", "Lote"
    APPOINTMENT = "appointment", "Consulta criada"


# Inscrições que ainda esperam alguma coisa do relógio.
LIVE_ENROLLMENT_STATUSES = (SequenceEnrollmentStatus.ACTIVE,)
