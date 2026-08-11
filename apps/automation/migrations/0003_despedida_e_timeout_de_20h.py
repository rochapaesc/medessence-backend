"""
A despedida do handoff automático e o timeout que cabe na janela (RF-FLW-11.1
a 11.3).

⚠️ Só mexe em quem está no DEFAULT ANTIGO. Quem digitou outro número na tela
escolheu aquele número, e sobrescrever seria desfazer configuração do gestor
(RF-FLW-11.3.2).
"""

from django.db import migrations

TIMEOUT_ANTIGO = 24
TIMEOUT_NOVO = 20


def aplicar(apps, schema_editor):
    from apps.automation.models.flow import (
        GOODBYE_REPROMPT,
        GOODBYE_TIMEOUT,
        MAX_BOT_MESSAGES,
    )

    Flow = apps.get_model("automation", "Flow")
    for flow in Flow.objects.all().iterator():
        politica = dict(flow.fallback or {})
        mudou = False

        # O timeout só desce se estiver no default antigo.
        if politica.get("on_timeout_hours") == TIMEOUT_ANTIGO:
            politica["on_timeout_hours"] = TIMEOUT_NOVO
            mudou = True

        # O teto de falas entra em quem não tem (RF-FLW-23.1): fluxo sem ele
        # ficaria sem trava nenhuma contra laço com outro robô.
        if not politica.get("max_bot_messages"):
            politica["max_bot_messages"] = MAX_BOT_MESSAGES
            mudou = True

        # As falas entram onde não existem: fluxo sem elas continuaria
        # entregando calado, que é o defeito que isto corrige.
        for chave, texto in (
            ("goodbye_reprompt", GOODBYE_REPROMPT),
            ("goodbye_timeout", GOODBYE_TIMEOUT),
        ):
            if not (politica.get(chave) or "").strip():
                politica[chave] = texto
                mudou = True

        if mudou:
            flow.fallback = politica
            flow.save(update_fields=["fallback"])


def desfazer(apps, schema_editor):
    """
    Volta o timeout e tira as falas. Não distingue quem já tinha 20 antes:
    desfazer é operação de emergência, e o valor de fábrica é o mais seguro.
    """
    Flow = apps.get_model("automation", "Flow")
    for flow in Flow.objects.all().iterator():
        politica = dict(flow.fallback or {})
        if politica.get("on_timeout_hours") == TIMEOUT_NOVO:
            politica["on_timeout_hours"] = TIMEOUT_ANTIGO
        politica.pop("goodbye_reprompt", None)
        politica.pop("goodbye_timeout", None)
        flow.fallback = politica
        flow.save(update_fields=["fallback"])


class Migration(migrations.Migration):
    dependencies = [("automation", "0002_flowrun_wake_at")]

    operations = [migrations.RunPython(aplicar, desfazer)]
