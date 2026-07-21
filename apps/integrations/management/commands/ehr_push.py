"""
Kill-switch do write-through (§10.2): liga/desliga `Clinic.ehr_push_enabled`
sem mexer em banco na mão - a trava do religamento por clínica.

Uso:
    python manage.py ehr_push medessence on
    python manage.py ehr_push medessence off
"""

from django.core.management.base import BaseCommand, CommandError

from apps.tenants.models import Clinic


class Command(BaseCommand):
    help = "Liga/desliga o push (write-through) ao EHR de uma clínica."

    def add_arguments(self, parser):
        parser.add_argument("clinic_slug", help="Slug da clínica (Clinic.slug).")
        parser.add_argument("state", choices=["on", "off"], help="on = escrita ligada.")

    def handle(self, *args, **options):
        try:
            clinic = Clinic.objects.get(slug=options["clinic_slug"])
        except Clinic.DoesNotExist as exc:
            raise CommandError(f"Clínica '{options['clinic_slug']}' não existe.") from exc

        if options["state"] == "on" and not clinic.ehr_provider:
            raise CommandError(
                f"Clínica '{clinic.slug}' não tem EHR configurado - nada a ligar."
            )

        clinic.ehr_push_enabled = options["state"] == "on"
        clinic.save(update_fields=["ehr_push_enabled", "updated_at"])
        self.stdout.write(
            self.style.SUCCESS(
                f"{clinic.slug}: ehr_push_enabled={clinic.ehr_push_enabled}"
            )
        )
