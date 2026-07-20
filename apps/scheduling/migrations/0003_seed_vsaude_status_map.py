"""
Mapa OFICIAL de status da vSaúde (P4 resolvida em 09/07/2026).

Semântica descoberta nas strings de localização do próprio sistema
(AbpUserConfiguration/GetAll → "Health.Appointment.Status.{n}.Title").
Padrão: unidade = quem executou a ação (1–9 paciente, X0 profissional,
X1 funcionário).

Decisão do produto: 100 ("Passou do horário" - expirou sem finalização)
NÃO conta como comparecimento → NO_SHOW.

A migration também remapeia consultas já importadas e recalcula o
denormalizado Patient.last_appointment_at.
"""

from django.db import migrations
from django.db.models import Max, Q
from django.utils import timezone

# código cru → (status normalizado, título oficial vSaúde)
VSAUDE_STATUS_MAP = {
    "1": ("scheduled", "Agendada pelo paciente"),
    "10": ("scheduled", "Agendada pelo profissional"),
    "11": ("scheduled", "Agendada por um funcionário"),
    "2": ("scheduled", "Remarcada pelo paciente"),
    "20": ("scheduled", "Remarcada pelo profissional"),
    "21": ("scheduled", "Remarcada por um funcionário"),
    "3": ("confirmed", "Confirmada pelo paciente"),
    "30": ("confirmed", "Confirmada pelo profissional"),
    "31": ("confirmed", "Confirmada pelo atendente"),
    "9": ("in_progress", "Aguardando atendimento"),
    "90": ("in_progress", "Em andamento"),
    "8": ("completed", "Finalizada pelo paciente"),
    "81": ("completed", "Finalizada pelo profissional"),
    "82": ("completed", "Finalizada por um funcionário"),
    "4": ("canceled", "Rejeitada pelo paciente"),
    "40": ("canceled", "Rejeitada pelo profissional"),
    "41": ("canceled", "Rejeitada por um funcionário"),
    "5": ("canceled", "Cancelada pelo paciente"),
    "50": ("canceled", "Cancelada pelo profissional"),
    "51": ("canceled", "Cancelada por um funcionário"),
    "7": ("canceled", "O profissional não compareceu"),
    "110": ("canceled", "Agendamento excluído/cancelado"),
    "6": ("no_show", "O paciente não compareceu"),
    "100": ("no_show", "Passou do horário"),
}

EXCLUDED_FROM_ATTENDANCE = ("canceled", "no_show")


def seed_and_remap(apps, schema_editor):
    EHRStatusMap = apps.get_model("scheduling", "EHRStatusMap")
    Appointment = apps.get_model("scheduling", "Appointment")
    Patient = apps.get_model("patients", "Patient")
    Clinic = apps.get_model("tenants", "Clinic")

    for source_status, (status, _title) in VSAUDE_STATUS_MAP.items():
        EHRStatusMap.objects.update_or_create(
            provider="vsaude",
            source_status=source_status,
            defaults={"status": status},
        )

    vsaude_clinics = list(
        Clinic.objects.filter(ehr_provider="vsaude").values_list("pk", flat=True)
    )
    if not vsaude_clinics:
        return

    # Remapeia consultas já importadas (update em massa não dispara signals)
    for source_status, (status, _title) in VSAUDE_STATUS_MAP.items():
        Appointment.objects.filter(
            clinic_id__in=vsaude_clinics, source_status=source_status
        ).exclude(status=status).update(status=status)

    # Recalcula o denormalizado last_appointment_at em lote
    now = timezone.now()
    patients = Patient.objects.filter(
        clinic_id__in=vsaude_clinics, deleted_at__isnull=True
    ).annotate(
        computed_last=Max(
            "appointments__starts_at",
            filter=Q(appointments__starts_at__lte=now)
            & Q(appointments__deleted_at__isnull=True)
            & ~Q(appointments__status__in=EXCLUDED_FROM_ATTENDANCE),
        )
    )
    to_update = []
    for patient in patients.iterator(chunk_size=500):
        if patient.last_appointment_at != patient.computed_last:
            patient.last_appointment_at = patient.computed_last
            to_update.append(patient)
        if len(to_update) >= 500:
            Patient.objects.bulk_update(to_update, ["last_appointment_at"])
            to_update = []
    if to_update:
        Patient.objects.bulk_update(to_update, ["last_appointment_at"])


class Migration(migrations.Migration):
    dependencies = [
        ("scheduling", "0002_alter_ehrstatusmap_provider_and_more"),
        ("patients", "0004_alter_patient_last_appointment_at"),
        ("tenants", "0001_initial"),
    ]

    operations = [
        migrations.RunPython(seed_and_remap, migrations.RunPython.noop),
    ]
