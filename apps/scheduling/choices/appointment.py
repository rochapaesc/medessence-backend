from django.db.models import TextChoices


class AppointmentStatus(TextChoices):
    """Status normalizado - o código cru do EHR fica em `source_status` e a
    tradução é administrável via EHRStatusMap (mapa oficial em 0003/0008)."""

    SCHEDULED = "scheduled", "Agendada"
    CONFIRMED = "confirmed", "Confirmada"
    WAITING = "waiting", "Aguardando atendimento"
    IN_PROGRESS = "in_progress", "Em atendimento"
    COMPLETED = "completed", "Realizada"
    NO_SHOW = "no_show", "Faltou"
    CANCELED = "canceled", "Cancelada"


# Estados anteriores ao atendimento. "Em atendimento" é LOCAL-only (RF-AGE-5:
# nenhum código do EHR produz in_progress), então pull/confirmação NUNCA
# regridem um in_progress local para um destes - só o avanço
# (completed/canceled/no_show) entra.
PRE_ATTENDANCE_STATUSES = (
    AppointmentStatus.SCHEDULED,
    AppointmentStatus.CONFIRMED,
    AppointmentStatus.WAITING,
)
