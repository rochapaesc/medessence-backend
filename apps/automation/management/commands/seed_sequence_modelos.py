"""
Cria uma sequência de cada MODELO do catálogo, para ver as telas com conteúdo.

    python manage.py seed_sequence_modelos --clinic 3
    python manage.py seed_sequence_modelos --clinic 3 --limpar

⚠️ Diferente do `seed_sequence_demo`, este comando **roda em clínica real** de
propósito, e por isso não cria contato, não inscreve ninguém e não dispara
nada. O que ele cria é inerte por três motivos somados:

  1. a sequência nasce **DESLIGADA**, e o motor só olha trilha ligada;
  2. a porta da consulta (`enroll_on_appointment`) só vale com a trilha
     ligada, então o espelho da vSaúde trazendo consulta nova não inscreve
     ninguém;
  3. os passos nascem apontando para fluxo em **rascunho**, e rascunho não
     dispara nem quando a hora chega.

`--limpar` desfaz tudo o que ele criou.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.automation.modelos import MODELOS, aplicar_modelo
from apps.automation.models import Flow, Sequence, SequenceEnrollment
from apps.tenants.models import Clinic


class Command(BaseCommand):
    help = "Cria uma sequência de cada modelo do catálogo (desligadas, sem ninguém dentro)."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True, help="ID da clínica")
        parser.add_argument(
            "--modelo",
            choices=sorted(MODELOS.keys()),
            help="Cria só a sequência deste modelo, em vez das quatro.",
        )
        parser.add_argument("--limpar", action="store_true", help="Remove o que foi criado")

    @transaction.atomic
    def handle(self, *args, **options):
        clinic = Clinic.objects.filter(pk=options["clinic"]).first()
        if clinic is None:
            raise CommandError(f"Clínica {options['clinic']} não encontrada.")

        nomes = [modelo["nome"] for modelo in MODELOS.values()]

        if options["limpar"]:
            return self._limpar(clinic, nomes)

        escolhido = options.get("modelo")
        catalogo = (
            {escolhido: MODELOS[escolhido]} if escolhido else MODELOS
        )

        criadas = 0
        for slug, modelo in catalogo.items():
            sequence, nova = Sequence.objects.get_or_create(
                clinic=clinic,
                name=modelo["nome"],
                defaults={
                    # ⚠️ Explícito, e não herdado do modelo: ligada, a trilha
                    # de consulta começaria a inscrever paciente de verdade na
                    # primeira sincronização da agenda.
                    "is_active": False,
                    "is_marketing": modelo["marketing"],
                    "enroll_on_appointment": modelo["por_consulta"],
                },
            )
            if not nova:
                consertados = self._reabrir_com_modelo(sequence, modelo)
                if consertados:
                    self.stdout.write(
                        f"  '{modelo['nome']}' já existia; {consertados} passo(s) "
                        "reaberto(s) com nó de modelo (era texto, e texto não "
                        "sai para quem está fora da janela)."
                    )
                else:
                    self.stdout.write(f"  '{modelo['nome']}' já existia, deixei como está.")
                continue

            passos = aplicar_modelo(sequence, slug)
            criadas += 1
            porta = "entra pela consulta" if modelo["por_consulta"] else "entra pela mão"
            tipo = "divulgação" if modelo["marketing"] else "atendimento"
            self.stdout.write(
                f"  {modelo['nome']}: {passos} passo(s) · {tipo} · {porta} · DESLIGADA"
            )

        self.stdout.write("")
        self.stdout.write(
            self.style.SUCCESS(
                f"{criadas} sequência(s) criada(s) em {clinic.name}. "
                "Todas desligadas, sem ninguém dentro, com as mensagens em rascunho."
            )
        )
        self.stdout.write(
            "Para ver: menu Sequências. Abrir uma mostra o painel, a coluna de "
            "passos e a lista do que falta antes de ligar."
        )

    def _reabrir_com_modelo(self, sequence, modelo):
        """
        Conserta trilha semeada ANTES da correção de 18/08, sem apagar nada.

        Os fluxos dos passos abriam com texto; texto não alcança quem está
        fora da janela de 24h (RF-SEQ-5.3). Reescreve a abertura para nó de
        template com o texto preservado em `suggested_body`. Só mexe em fluxo
        RASCUNHO com o nome do semeador, e quem está inscrito continua dentro.
        """
        consertados = 0
        for passo in sequence.steps.select_related("flow"):
            flow = passo.flow
            if flow.status != "draft" or not flow.name.startswith(f"{sequence.name}: "):
                continue
            versao = flow.current_version
            if versao is None:
                continue
            nos = (versao.graph or {}).get("nodes") or []
            fala = next((n for n in nos if n.get("type") == "send_message"), None)
            if fala is None:
                continue
            fala["type"] = "send_template"
            fala["config"] = {
                "template_name": "",
                "variables": {},
                "suggested_body": (fala.get("config") or {}).get("text", ""),
            }
            versao.save(update_fields=["graph", "updated_at"])
            consertados += 1
        return consertados

    def _limpar(self, clinic, nomes):
        trilhas = Sequence.all_objects.filter(clinic=clinic, name__in=nomes)
        dentro = SequenceEnrollment.all_objects.filter(sequence__in=trilhas).count()
        if dentro:
            # Trilha com gente dentro não foi só este comando que mexeu.
            raise CommandError(
                f"Há {dentro} inscrição(ões) nessas trilhas. Não vou apagar por cima disso."
            )

        # Os fluxos de rascunho que os passos criaram levam o nome da trilha.
        fluxos = Flow.objects.none()
        for nome in nomes:
            fluxos = fluxos | Flow.objects.filter(clinic=clinic, name__startswith=f"{nome}: ")

        quantos = (trilhas.count(), fluxos.count())
        for trilha in trilhas:
            trilha.hard_delete()
        fluxos.delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Removidas {quantos[0]} sequência(s) e {quantos[1]} fluxo(s) de rascunho."
            )
        )
