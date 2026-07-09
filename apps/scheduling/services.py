from django.utils import timezone


def recalculate_last_appointment(patient) -> None:
    """
    Recalcula o denormalizado `Patient.last_appointment_at` (RF-PAC-2):
    a última consulta PASSADA que conta (exclui canceladas e faltas).

    Chamado pelos signals de Appointment e pelo pull da agenda (fase do
    adapter). Uma consulta futura não torna o paciente "ativo".
    """
    from apps.scheduling.choices import AppointmentStatus

    last = (
        patient.appointments.filter(starts_at__lte=timezone.now())
        .exclude(status__in=[AppointmentStatus.CANCELED, AppointmentStatus.NO_SHOW])
        .order_by("-starts_at")
        .values_list("starts_at", flat=True)
        .first()
    )
    if patient.last_appointment_at != last:
        patient.last_appointment_at = last
        patient.save(update_fields=["last_appointment_at", "updated_at"])
