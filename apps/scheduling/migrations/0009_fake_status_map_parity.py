# Paridade do mapa de status do provider FAKE com a vSaúde real (28/07/2026).
#
# O mapa do FAKE tinha só 3 códigos (10/30/81) enquanto o adapter emitia 4 -
# então todo pull de clínica fake terminava com
# `unmapped_statuses: ['90', '100']`, e as consultas caíam no fallback
# `scheduled` (o pull não descarta o que não sabe mapear).
#
# Duas correções, uma em cada ponta:
#   - o adapter deixou de emitir "90": esse código NÃO EXISTE na vSaúde (erro
#     do levantamento original, confirmado em 21/07/2026 e removido do mapa
#     vSaúde pela 0008). Um fake que emite código impossível faz o dev
#     exercitar um caminho que produção nunca produz;
#   - o mapa do FAKE passa a cobrir todos os códigos que o adapter emite, com
#     a MESMA semântica oficial da vSaúde (P4/migration 0003).
#
# As consultas já espelhadas com 90/100 são recalculadas: 100 vira no_show
# ("passou do horário" não conta como comparecimento - decisão do usuário em
# 09/07/2026) e 90, que não deveria existir, vira `completed`, que era a
# intenção do ciclo antigo ("realizada").

from django.db import migrations

# Mesma semântica do mapa vSaúde (0003), cobrindo TUDO que o fake gera:
# o ciclo do pull (STATUS_CYCLE) e os códigos que as transições gravam
# (TRANSITION_CODES). Cobrir só o ciclo deixaria o buraco no caminho de
# escrita — que é justamente o que o dev exercita depois de agir na tela.
FAKE_MAP = {
    # ciclo do pull
    "10": "scheduled",
    "30": "confirmed",
    "81": "completed",
    "100": "no_show",
    "51": "canceled",
    # transições (fake TRANSITION_CODES) - "in_progress" não tem código de
    # propósito: é estado local-only (RF-AGE-5, D1).
    "9": "waiting",
    "50": "canceled",
    "6": "no_show",
}


def seed_fake_map(apps, schema_editor):
    EHRStatusMap = apps.get_model("scheduling", "EHRStatusMap")
    Appointment = apps.get_model("scheduling", "Appointment")

    for source_status, status in FAKE_MAP.items():
        EHRStatusMap.objects.update_or_create(
            provider="fake",
            source_status=source_status,
            defaults={"status": status},
        )

    fake_appointments = Appointment.objects.filter(clinic__ehr_provider="fake")
    for source_status, status in FAKE_MAP.items():
        fake_appointments.filter(source_status=source_status).update(status=status)
    # Resíduo do ciclo antigo: "90" era o "realizada" que o fake emitia.
    fake_appointments.filter(source_status="90").update(status="completed")


def unseed_fake_map(apps, schema_editor):
    EHRStatusMap = apps.get_model("scheduling", "EHRStatusMap")
    # Volta ao mapa mínimo anterior (10/30/81 já existiam antes desta migration).
    EHRStatusMap.objects.filter(
        provider="fake", source_status__in=["100", "51"]
    ).delete()


class Migration(migrations.Migration):

    dependencies = [
        ("scheduling", "0008_remap_waiting_status"),
    ]

    operations = [
        migrations.RunPython(seed_fake_map, unseed_fake_map),
    ]
