"""
Sonda de calibração do EHR (Fase 2 do write-path, §10.2): chama UMA rota do
provedor da clínica com o payload dado e imprime a resposta crua - para
descobrir payloads aceitos, códigos de status gravados e mensagens de erro
reais SEM passar pelo outbox (SyncOperation).

Uso:
    manage.py vsaude_probe medessence PatientService/Create \
        --method post --data '{"name": "Fulano", ...}'
    manage.py vsaude_probe medessence ScheduleService/Get \
        --method get --data '{"id": "<guid>"}'
"""

import json

from django.core.management.base import BaseCommand, CommandError

from apps.integrations.ehr.exceptions import EHRError
from apps.integrations.ehr.registry import get_ehr_provider
from apps.tenants.models import Clinic


class Command(BaseCommand):
    help = "Chama uma rota do EHR da clínica com payload dado e imprime a resposta crua."

    def add_arguments(self, parser):
        parser.add_argument("clinic_slug", help="Slug da clínica (Clinic.slug).")
        parser.add_argument("route", help="Rota relativa, ex.: PatientService/Create.")
        parser.add_argument(
            "--method",
            choices=["get", "post", "put", "delete"],
            default="post",
            help="Verbo HTTP (default: post).",
        )
        parser.add_argument(
            "--data",
            default="{}",
            help="Payload JSON (body p/ post/put; querystring p/ get/delete).",
        )
        parser.add_argument(
            "--params",
            default=None,
            help="Querystring JSON adicional p/ POST (ex.: Search?keyword=).",
        )

    def handle(self, *args, **options):
        try:
            clinic = Clinic.objects.get(slug=options["clinic_slug"])
        except Clinic.DoesNotExist as exc:
            raise CommandError(f"Clínica '{options['clinic_slug']}' não existe.") from exc

        provider = get_ehr_provider(clinic)
        client = getattr(provider, "client", None)
        if client is None:
            raise CommandError("Provider da clínica não expõe client HTTP (é fake?).")

        try:
            payload = json.loads(options["data"])
        except json.JSONDecodeError as exc:
            raise CommandError(f"--data não é JSON válido: {exc}") from exc

        method = options["method"]
        try:
            if method == "post" and options["params"]:
                result = client.post(
                    options["route"], payload, params=json.loads(options["params"])
                )
            else:
                result = getattr(client, method)(options["route"], payload)
        except EHRError as exc:
            self.stderr.write(self.style.ERROR(f"ERRO: {exc}"))
            return

        self.stdout.write(json.dumps(result, ensure_ascii=False, indent=1, default=str))
