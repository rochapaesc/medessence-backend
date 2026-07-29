from django.db.models import TextChoices


class NotificationKind(TextChoices):
    """
    Os quatro blocos da central. A ordem de declaração é a ordem de exibição:
    o feed sai ordenado por severidade, não por horário.
    """

    # Primeiro da lista de propósito: com o canal fora, NADA entra nem sai —
    # é mais grave que qualquer falha de sincronização.
    CHANNEL_DOWN = "channel_down", "WhatsApp desconectado"
    SYNC_FAILED = "sync_failed", "Falha de sincronização"
    NO_SHOW = "no_show", "Falta para recuperar"
    PENDING_OUTCOME = "pending_outcome", "Consulta sem desfecho"
    APPOINTMENT_TODAY = "appointment_today", "Hoje na agenda"


class NotificationSeverity(TextChoices):
    DANGER = "danger", "Erro"
    WARNING = "warning", "Atenção"
    INFO = "info", "Informativo"
