from django.utils import timezone


def recalculate_last_appointment(patient) -> None:
    """
    Recalcula os denormalizados de agenda do paciente (RF-PAC-2):

    - `last_appointment_at`: última consulta PASSADA que conta (exclui
      canceladas e faltas) — define o "compareceu na janela".
    - `next_appointment_at`: próxima consulta FUTURA agendada (exclui
      canceladas/faltas) — um retorno marcado mantém o paciente ativo e
      fora do balde de reativação.

    Chamado pelos signals de Appointment e pelo pull da agenda (fase do adapter).
    """
    from apps.scheduling.choices import AppointmentStatus

    now = timezone.now()
    dates = list(
        patient.appointments.exclude(
            status__in=[AppointmentStatus.CANCELED, AppointmentStatus.NO_SHOW]
        )
        .order_by("starts_at")
        .values_list("starts_at", flat=True)
    )
    past = [d for d in dates if d <= now]
    future = [d for d in dates if d > now]
    last = past[-1] if past else None
    nxt = future[0] if future else None

    fields = []
    if patient.last_appointment_at != last:
        patient.last_appointment_at = last
        fields.append("last_appointment_at")
    if patient.next_appointment_at != nxt:
        patient.next_appointment_at = nxt
        fields.append("next_appointment_at")
    if fields:
        patient.save(update_fields=[*fields, "updated_at"])
