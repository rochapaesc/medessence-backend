"""
Expurgo do log de auditoria (§15).

O log cresce para sempre: guardar acesso indefinidamente é o oposto do que a
LGPD pede (minimização). A retenção padrão é de 5 anos.

Uso:
    manage.py purge_audit_logs                 # só mostra o que apagaria
    manage.py purge_audit_logs --apply         # apaga de verdade
    manage.py purge_audit_logs --years 3 --apply
    manage.py purge_audit_logs --clinic medessence --apply

Apagar log é IRREVERSÍVEL e o registro é a prova de quem acessou o quê -
por isso o padrão é o ensaio, e apagar exige `--apply` explícito.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.core.models import AuditLog
from apps.tenants.models import Clinic

DEFAULT_YEARS = 5
CHUNK = 5_000


class Command(BaseCommand):
    help = "Apaga logs de auditoria mais antigos que a janela de retenção."

    def add_arguments(self, parser):
        parser.add_argument(
            "--years",
            type=int,
            default=DEFAULT_YEARS,
            help=f"Janela de retenção em anos (default: {DEFAULT_YEARS}).",
        )
        parser.add_argument(
            "--clinic",
            help="Slug da clínica. Sem isto, vale para todas (inclui logs globais).",
        )
        parser.add_argument(
            "--apply",
            action="store_true",
            help="Apaga de verdade. Sem esta flag, só relata.",
        )

    def handle(self, *args, **options):
        years = options["years"]
        if years < 1:
            raise CommandError("--years precisa ser pelo menos 1.")

        cutoff = timezone.now() - timedelta(days=365 * years)
        queryset = AuditLog.objects.filter(timestamp__lt=cutoff)

        if options["clinic"]:
            try:
                clinic = Clinic.objects.get(slug=options["clinic"])
            except Clinic.DoesNotExist as exc:
                raise CommandError(f"Clínica '{options['clinic']}' não existe.") from exc
            queryset = queryset.filter(clinic=clinic)

        total = queryset.count()
        escopo = options["clinic"] or "todas as clínicas"
        self.stdout.write(
            f"{total} evento(s) anteriores a {cutoff.date().isoformat()} "
            f"({years} anos) em {escopo}."
        )
        if total == 0:
            return

        if not options["apply"]:
            oldest = queryset.order_by("timestamp").first()
            self.stdout.write(
                self.style.WARNING(
                    f"Ensaio: nada foi apagado. Mais antigo: "
                    f"{oldest.timestamp.date().isoformat()}. Use --apply para apagar."
                )
            )
            return

        # Em blocos: uma tabela de auditoria antiga pode ter milhões de linhas,
        # e um DELETE único seguraria a transação (e a memória) por muito tempo.
        apagados = 0
        while True:
            ids = list(queryset.values_list("pk", flat=True)[:CHUNK])
            if not ids:
                break
            apagados += AuditLog.objects.filter(pk__in=ids).delete()[0]
            self.stdout.write(f"  {apagados}/{total}...")

        self.stdout.write(self.style.SUCCESS(f"{apagados} evento(s) apagados."))
