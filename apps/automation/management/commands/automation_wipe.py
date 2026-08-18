"""
Apaga TODOS os fluxos e sequências de uma clínica, para recomeçar do zero.

    python manage.py automation_wipe --clinic 3 --confirmo

⚠️ É a limpeza total que os `--limpar` dos seeders não fazem de propósito: eles
só removem o que semearam, e se recusam quando encontram inscrição. Este
comando existe para o "quero começar do zero" dito por quem gerencia, e por
isso exige `--confirmo` e imprime o que apagou.

O que ele NÃO toca: conversas, mensagens, pacientes e contatos. O rastro do
que o robô disse nas conversas fica; o que sai é o motor (fluxos, versões,
execuções, sequências, passos, inscrições, disparos).
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.automation.models import (
    Flow,
    FlowRun,
    Sequence,
    SequenceDispatch,
    SequenceEnrollment,
    SequenceStep,
)
from apps.tenants.models import Clinic


class Command(BaseCommand):
    help = "Apaga todos os fluxos e sequências de uma clínica (irreversível)."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True, help="ID da clínica")
        parser.add_argument(
            "--confirmo",
            action="store_true",
            help="Obrigatório: confirma que é para apagar tudo mesmo.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        clinic = Clinic.objects.filter(pk=options["clinic"]).first()
        if clinic is None:
            raise CommandError(f"Clínica {options['clinic']} não encontrada.")
        if not options["confirmo"]:
            raise CommandError(
                "Isto apaga TODOS os fluxos e sequências da clínica "
                f"{clinic.name}. Rode de novo com --confirmo se for isso mesmo."
            )

        # ⚠️ A ordem importa: disparos antes de inscrições, passos antes de
        # fluxos (SequenceStep.flow é RESTRICT), e `all_objects` + hard_delete
        # em tudo, senão o soft delete deixa linha morta segurando FK.
        disparos = SequenceDispatch.all_objects.filter(
            enrollment__sequence__clinic=clinic
        )
        n_disparos = disparos.count()
        disparos.hard_delete()

        inscricoes = SequenceEnrollment.all_objects.filter(sequence__clinic=clinic)
        n_inscricoes = inscricoes.count()
        inscricoes.hard_delete()

        passos = SequenceStep.all_objects.filter(sequence__clinic=clinic)
        n_passos = passos.count()
        passos.hard_delete()

        trilhas = list(Sequence.all_objects.filter(clinic=clinic))
        for trilha in trilhas:
            trilha.hard_delete()

        n_runs = FlowRun.objects.filter(flow__clinic=clinic).count()
        fluxos = list(Flow.all_objects.filter(clinic=clinic))
        for fluxo in fluxos:
            # Versões e execuções caem em cascata; os eventos caem com elas.
            fluxo.hard_delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"{clinic.name}: {len(trilhas)} sequência(s), {n_passos} passo(s), "
                f"{n_inscricoes} inscrição(ões), {n_disparos} disparo(s), "
                f"{len(fluxos)} fluxo(s) e {n_runs} execução(ões) apagados."
            )
        )
        self.stdout.write("Conversas, mensagens, pacientes e contatos não foram tocados.")
