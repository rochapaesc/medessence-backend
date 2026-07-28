"""
Sonda de ENVIO do WhatsApp (calibração) — mesmo papel do `vsaude_probe`.

Fala DIRETO com o adapter, furando a API REST e a validação de janela de 24h
do domínio: na calibração a gente precisa provar o transporte, e a janela
local depende de termos registrado um inbound — o que só acontece com webhook
configurado.

    manage.py wa_send medessence --to 5589999228477 --template hello_world --lang en_US
    manage.py wa_send medessence --to 5589999228477 --text "oi"   (exige janela aberta)

NÃO grava Message/Conversation: é transporte puro, fora do domínio. Mensagem
enviada por aqui não aparece no Inbox.
"""

import logging
import re

from django.core.management.base import BaseCommand, CommandError

from apps.inbox.models import Channel
from apps.tenants.models import Clinic


class Command(BaseCommand):
    help = "Envia uma mensagem de teste pelo canal WhatsApp da clínica (calibração)."

    def add_arguments(self, parser):
        parser.add_argument("clinic_slug")
        parser.add_argument(
            "--to", required=True, help="Destino em E.164 sem '+' (ex.: 5589999228477)."
        )
        parser.add_argument("--text", help="Texto livre — só com a janela de 24h aberta.")
        parser.add_argument("--template", help="Nome de um template aprovado.")
        parser.add_argument("--lang", default="pt_BR", help="Idioma do template.")

    def handle(self, *args, **options):
        for nome in ("httpx", "httpcore", "httpcore.http11", "pywa"):
            logging.getLogger(nome).setLevel(logging.WARNING)

        texto = options.get("text")
        template = options.get("template")
        if bool(texto) == bool(template):
            raise CommandError("Informe --text OU --template (um dos dois).")

        to = re.sub(r"\D", "", options["to"])
        if not to:
            raise CommandError("--to precisa ser E.164 só com dígitos (ex.: 5589999228477).")

        try:
            clinic = Clinic.objects.get(slug=options["clinic_slug"])
        except Clinic.DoesNotExist as exc:
            raise CommandError(f"Clínica '{options['clinic_slug']}' não existe.") from exc

        channel = Channel.objects.filter(clinic=clinic).first()
        if channel is None:
            raise CommandError(f"{clinic.name} não tem canal. Rode `wa_channel` antes.")

        from apps.integrations.whatsapp.exceptions import WhatsAppError
        from apps.integrations.whatsapp.registry import get_whatsapp_provider

        provider = get_whatsapp_provider(channel)

        # Envio real, com custo: deixa explícito o que vai sair e para quem.
        alvo = f"+{to}"
        oque = f"template '{template}' ({options['lang']})" if template else f"texto: {texto!r}"
        self.stdout.write(f"Enviando {oque}\n  de {channel.display_number or channel.phone_number_id}\n  para {alvo}")

        try:
            if template:
                result = provider.send_template(to, template, options["lang"])
            else:
                result = provider.send_text(to, texto)
        except WhatsAppError as exc:
            raise CommandError(f"A Meta recusou: {exc}") from exc

        self.stdout.write(self.style.SUCCESS(f"ENVIADO. wamid: {result.provider_message_id}"))
        self.stdout.write(
            "\nO status de entrega (delivered/read) só chega por webhook — "
            "se ele ainda não aponta para cá, confira a entrega no celular."
        )
