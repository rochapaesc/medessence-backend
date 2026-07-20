"""
Disparo manual de pulls - para desenvolvimento e operação.

Uso:
    python manage.py sync clinica-1                  # todos, na ordem certa
    python manage.py sync clinica-1 --kind patients
    python manage.py sync clinica-1 --kind appointments
"""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.integrations.services import PULLS, pull_all
from apps.tenants.models import Clinic


class Command(BaseCommand):
    help = "Executa o pull do EHR para uma clínica (catalogs, patients, appointments)."

    def add_arguments(self, parser):
        parser.add_argument("clinic_slug", help="Slug da clínica (Clinic.slug).")
        parser.add_argument(
            "--kind",
            choices=[*PULLS.keys(), "all"],
            default="all",
            help="Qual pull executar (default: all, na ordem catalogs → patients → appointments).",
        )
        parser.add_argument(
            "--from",
            dest="start",
            default=None,
            help="Só p/ appointments: início da janela (YYYY-MM-DD). Útil p/ backfill histórico.",
        )
        parser.add_argument(
            "--to",
            dest="end",
            default=None,
            help="Só p/ appointments: fim da janela (YYYY-MM-DD).",
        )

    def handle(self, *args, **options):
        from datetime import date

        clinic = Clinic.objects.filter(slug=options["clinic_slug"]).first()
        if clinic is None:
            raise CommandError(f"Clínica '{options['clinic_slug']}' não encontrada.")
        if not clinic.ehr_provider:
            raise CommandError(
                f"Clínica '{clinic.slug}' sem EHR configurado - defina ehr_provider "
                "e ehr_credentials no admin."
            )

        kind = options["kind"]
        window = {}
        if options["start"] or options["end"]:
            if kind != "appointments":
                raise CommandError("--from/--to só valem com --kind appointments.")
            if options["start"]:
                window["start"] = date.fromisoformat(options["start"])
            if options["end"]:
                window["end"] = date.fromisoformat(options["end"])

        if kind == "all":
            runs = pull_all(clinic)
        else:
            runs = [PULLS[kind](clinic, **window)]

        for run in runs:
            duration = ""
            if run.started_at and run.finished_at:
                duration = f" em {(run.finished_at - run.started_at).total_seconds():.1f}s"
            self.stdout.write(self.style.SUCCESS(f"[{run.get_kind_display()}]{duration}"))
            self.stdout.write(f"  {json.dumps(run.stats, ensure_ascii=False)}")
            if run.error:
                self.stdout.write(self.style.ERROR(f"  erro: {run.error}"))
