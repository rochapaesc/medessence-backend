from django.db.models import TextChoices


class AppointmentStatus(TextChoices):
    """Status normalizado — o código cru do EHR fica em `source_status` e a
    tradução é administrável via EHRStatusMap (P4: calibrar 10/81/90/100)."""

    SCHEDULED = "scheduled", "Agendada"
    CONFIRMED = "confirmed", "Confirmada"
    IN_PROGRESS = "in_progress", "Em atendimento"
    COMPLETED = "completed", "Realizada"
    NO_SHOW = "no_show", "Faltou"
    CANCELED = "canceled", "Cancelada"
