"""
Simula uma mensagem recebida no WhatsApp - dev/QA sem número real.

Injeta um payload no formato Meta e roda o pipeline SÍNCRONO
(store → parse → ingestão), como se o webhook tivesse chegado. Útil para
exercitar conversa/thread antes da Datafy real (canal FAKE ou DATAFY).

Uso:
    python manage.py wa_simulate clinica-1 --from 5585999998888 --body "Oi, posso remarcar?"
"""

from django.core.management.base import BaseCommand, CommandError

from apps.inbox.choices import WebhookSource
from apps.inbox.models import Channel, WebhookEvent
from apps.inbox.tasks import process_whatsapp_webhook
from apps.integrations.whatsapp.fake.adapter import build_inbound_payload
from apps.tenants.models import Clinic


class Command(BaseCommand):
    help = "Simula um inbound de WhatsApp e roda a ingestão (dev/QA)."

    def add_arguments(self, parser):
        parser.add_argument("clinic_slug", help="Slug da clínica.")
        parser.add_argument("--from", dest="wa_id", default="5585999990001", help="Número (wa_id).")
        parser.add_argument("--name", default="Contato Fake", help="Nome no WhatsApp.")
        parser.add_argument("--body", default="Olá! Mensagem de teste.", help="Texto da mensagem.")

    def handle(self, *args, **options):
        clinic = Clinic.objects.filter(slug=options["clinic_slug"]).first()
        if clinic is None:
            raise CommandError(f"Clínica '{options['clinic_slug']}' não encontrada.")
        channel = Channel.objects.filter(clinic=clinic).first()
        if channel is None:
            raise CommandError(f"Clínica '{clinic.slug}' sem canal - rode `seed --only inbox`.")

        payload = build_inbound_payload(
            wa_id=options["wa_id"], body=options["body"], name=options["name"]
        )
        # Mesmo caminho do webhook real: grava o log cru e roda a task
        # (síncrona aqui) - que faz parse + ingestão e marca `processed_at`.
        event = WebhookEvent.objects.create(
            source=WebhookSource.META, clinic=clinic, payload=payload
        )
        stats = process_whatsapp_webhook(event.pk, channel.pk)
        self.stdout.write(self.style.SUCCESS(f"Ingestão: {stats}"))
