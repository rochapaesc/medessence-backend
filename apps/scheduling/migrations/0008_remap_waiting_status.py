# Correção do mapa de status da vSaúde para a fase de escrita (21/07/2026):
#
# - O código 9 (título oficial "Aguardando atendimento") estava mapeado para
#   `in_progress` porque o seed (0003) é anterior ao status `waiting` (0007).
#   Remapeia para `waiting` — é o código que a rota ScheduleService/Waiting
#   deve gravar (confirmar na calibração ao vivo).
# - O código 90 ("Em andamento") NÃO existe na vSaúde real — erro do
#   levantamento original, confirmado em 21/07/2026. Sai do mapa: NENHUM
#   código do EHR produz `in_progress`; o estado é exclusivamente local
#   (RF-AGE-5), protegido pela guarda anti-regressão do pull/push.
# - Recalcula as consultas espelhadas de clínicas vSaúde com esses códigos.

from django.db import migrations


def remap_waiting(apps, schema_editor):
    EHRStatusMap = apps.get_model("scheduling", "EHRStatusMap")
    Appointment = apps.get_model("scheduling", "Appointment")

    EHRStatusMap.objects.filter(provider="vsaude", source_status="9").update(
        status="waiting"
    )
    EHRStatusMap.objects.filter(provider="vsaude", source_status="90").delete()

    Appointment.objects.filter(
        clinic__ehr_provider="vsaude", source_status__in=("9", "90")
    ).update(status="waiting")


def unmap_waiting(apps, schema_editor):
    EHRStatusMap = apps.get_model("scheduling", "EHRStatusMap")
    Appointment = apps.get_model("scheduling", "Appointment")

    EHRStatusMap.objects.filter(provider="vsaude", source_status="9").update(
        status="in_progress"
    )
    EHRStatusMap.objects.update_or_create(
        provider="vsaude",
        source_status="90",
        defaults={"status": "in_progress"},
    )
    Appointment.objects.filter(
        clinic__ehr_provider="vsaude", source_status__in=("9", "90")
    ).update(status="in_progress")


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0007_alter_appointment_status_alter_ehrstatusmap_status"),
    ]

    operations = [
        migrations.RunPython(remap_waiting, unmap_waiting),
    ]
