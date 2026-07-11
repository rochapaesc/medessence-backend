"""
Backfill de `Patient.next_appointment_at`: próxima consulta futura agendada
(exclui canceladas/faltas). Vem junto com a nova definição de "ativo", que
passa a considerar retorno marcado (sem viés no balde de reativação).
"""

from django.db import migrations
from django.db.models import Min, Q
from django.utils import timezone


def backfill_next(apps, schema_editor):
    Patient = apps.get_model("patients", "Patient")
    now = timezone.now()
    qs = Patient.objects.annotate(
        computed_next=Min(
            "appointments__starts_at",
            filter=Q(appointments__starts_at__gt=now)
            & Q(appointments__deleted_at__isnull=True)
            & ~Q(appointments__status__in=["canceled", "no_show"]),
        )
    )
    to_update = []
    for patient in qs.iterator():
        if patient.next_appointment_at != patient.computed_next:
            patient.next_appointment_at = patient.computed_next
            to_update.append(patient)
        if len(to_update) >= 500:
            Patient.objects.bulk_update(to_update, ["next_appointment_at"])
            to_update = []
    if to_update:
        Patient.objects.bulk_update(to_update, ["next_appointment_at"])


class Migration(migrations.Migration):
    dependencies = [("patients", "0005_patient_next_appointment_at")]
    operations = [migrations.RunPython(backfill_next, migrations.RunPython.noop)]
