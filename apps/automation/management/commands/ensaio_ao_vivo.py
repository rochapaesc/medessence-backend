"""
Ensaio da sequência NA CLÍNICA DE VERDADE, com um número só: o seu.

    python manage.py ensaio_ao_vivo --clinic 3 --numero 5589XXXXXXXX
    python manage.py ensaio_ao_vivo --clinic 3 --situacao
    python manage.py ensaio_ao_vivo --clinic 3 --ligar --confirmo
    python manage.py ensaio_ao_vivo --clinic 3 --limpar

⚠️ **Aqui as mensagens SAEM DE VERDADE.** O `ensaio_de_sequencia` roda em
clínica de mentira e não alcança ninguém; este é o contrário, e existe para
ver a trilha chegando no celular. Por isso ele:

  · aceita UM número, que precisa já ser contato da clínica;
  · usa só template APROVADO na conta da clínica;
  · nasce DESLIGADO, e ligar exige `--confirmo`;
  · não cria consulta nem toca em ficha (inscreve pelo CONTATO, não pelo
    paciente), para não sujar agenda nem prontuário de verdade.

⚠️ O que este ensaio NÃO consegue provar enquanto o webhook apontar para a
produção: **entregue, lida e resposta**. O envio é chamada direta à Meta e
funciona; o retorno dela chega no servidor de produção, não aqui. No painel
local o disparo para em "enviado", e isso não é defeito.
"""

import zoneinfo
from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.automation.choices import EnrollmentSource, SequenceEnrollmentStatus
from apps.automation.models import (
    Flow,
    Sequence,
    SequenceDispatch,
    SequenceEnrollment,
    SequenceStep,
)
from apps.automation.modelos import criar_fluxo_de_aviso
from apps.automation.sequences import inscrever, recalcular
from apps.inbox.models import Channel, Conversation, WhatsAppTemplate
from apps.patients.models import Contact, PatientContact
from apps.tenants.models import Clinic

TRILHA = "Ensaio ao vivo (teste)"

# Os três do resgate, que a clínica já aprovou na Meta. Template aprovado é
# requisito e não preferência: a trilha fala com quem está fora da janela de
# 24h, e texto livre ali fica segurado para sempre (RF-SEQ-5.3).
PASSOS = [
    ("Primeiro convite", "resgate_de_inativos_primeiro_convite"),
    ("Segunda tentativa", "resgate_de_inativos_segunda_tentativa"),
    ("Última tentativa", "resgate_de_inativos_ultima_tentativa"),
]


class Command(BaseCommand):
    help = "Ensaio da sequência na clínica real, com um número só."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True)
        parser.add_argument("--numero", help="O WhatsApp que vai receber. Só um.")
        parser.add_argument(
            "--paciente",
            type=int,
            help=(
                "Inscreve pela FICHA deste paciente, e não por um número solto. "
                "É o caminho que a clínica usa de verdade, e o que decide por "
                "qual dos números dele a trilha vai falar."
            ),
        )
        parser.add_argument("--minutos", type=int, default=3)
        parser.add_argument("--ligar", action="store_true", help="Liga e começa a disparar.")
        parser.add_argument("--confirmo", action="store_true", help="Confirma o envio real.")
        parser.add_argument("--situacao", action="store_true")
        parser.add_argument("--limpar", action="store_true")

    def handle(self, *args, **opts):
        clinic = self._clinica(opts["clinic"])
        if opts["limpar"]:
            return self._limpar(clinic)
        if opts["situacao"]:
            return self._situacao(clinic)
        if opts["ligar"]:
            return self._ligar(clinic, opts["minutos"], confirmou=opts["confirmo"])
        if not opts["numero"] and not opts["paciente"]:
            raise CommandError("Falta o --numero ou o --paciente de quem vai receber.")
        self._montar(
            clinic, opts["numero"], opts["minutos"], paciente_id=opts["paciente"]
        )

    def _clinica(self, pk):
        try:
            return Clinic.objects.get(pk=pk)
        except Clinic.DoesNotExist:
            raise CommandError(f"Clínica {pk} não existe.")

    def _fuso(self, clinic):
        return zoneinfo.ZoneInfo(clinic.timezone or "America/Sao_Paulo")

    def _contato(self, clinic, numero=None):
        if numero:
            # Não cria contato: em clínica de verdade, inventar destinatário é
            # como se manda mensagem para quem não pediu.
            contato = Contact.objects.filter(
                clinic=clinic, wa_id__endswith=numero[-8:]
            ).first()
            if contato is None:
                raise CommandError(
                    f"O número {numero} não é contato da clínica. Mande uma "
                    "mensagem para ele pelo Inbox primeiro, ou confira o número."
                )
            return contato
        inscricao = SequenceEnrollment.objects.filter(
            sequence__clinic=clinic, sequence__name=TRILHA
        ).select_related("contact").first()
        if inscricao is None:
            raise CommandError("O ensaio não está montado.")
        return inscricao.contact

    @transaction.atomic
    def _montar(self, clinic, numero, minutos, *, paciente_id=None):
        from apps.automation.sequences import SemContato, contato_do_paciente
        from apps.patients.models import Patient

        paciente = None
        if paciente_id:
            paciente = Patient.objects.filter(clinic=clinic, pk=paciente_id).first()
            if paciente is None:
                raise CommandError(f"Paciente {paciente_id} não existe nesta clínica.")
            try:
                contato = contato_do_paciente(paciente)
            except SemContato:
                raise CommandError(f"{paciente.name} não tem número vinculado.")
        else:
            contato = self._contato(clinic, numero)
        canal = Channel.objects.filter(clinic=clinic, is_test=False).first()
        if canal is None:
            raise CommandError("A clínica não tem canal de WhatsApp.")

        faltando = [
            t
            for _nome, t in PASSOS
            if not WhatsAppTemplate.objects.filter(
                clinic=clinic, name=t, status="APPROVED"
            ).exists()
        ]
        if faltando:
            raise CommandError(
                "Estes modelos não estão aprovados nesta clínica: "
                + ", ".join(faltando)
                + ". Sem modelo aprovado o passo fica segurado para sempre."
            )

        self._zerar(clinic)
        trilha, _ = Sequence.objects.get_or_create(
            clinic=clinic,
            name=TRILHA,
            defaults={
                "is_active": False,  # nasce desligada: ligar é gesto seu
                "is_marketing": True,
                "exit_on_reply": True,
                "exit_on_appointment": True,
            },
        )
        trilha.is_active = False
        trilha.save(update_fields=["is_active", "updated_at"])

        agora = timezone.localtime(timezone.now(), self._fuso(clinic))
        for ordem, (nome, template) in enumerate(PASSOS, start=1):
            flow = criar_fluxo_de_aviso(
                clinic,
                nome=f"{TRILHA}: {nome}",
                template_name=template,
                variables={},
                flow=Flow.objects.filter(clinic=clinic, name=f"{TRILHA}: {nome}").first(),
            )
            passo, _ = SequenceStep.objects.get_or_create(
                sequence=trilha, order=ordem, defaults={"offset_days": 0, "flow": flow}
            )
            passo.name = nome
            passo.offset_days = 0
            passo.send_time = (agora + timedelta(minutes=minutos * ordem)).time().replace(
                second=0, microsecond=0
            )
            passo.flow = flow
            passo.save()

        inscrever(
            trilha,
            contato,
            source=EnrollmentSource.PATIENT_RECORD
            if paciente
            else EnrollmentSource.BATCH,
            patient=paciente,
        )

        if paciente:
            # A prova de para onde a trilha vai falar, ANTES de ela falar. Foi
            # exatamente isto que faltou quando a mensagem saiu pelo número de
            # outra pessoa da mesma ficha.
            self.stdout.write("")
            self.stdout.write(f"  Ficha: {paciente.name}")
            for pc in PatientContact.objects.filter(patient=paciente).select_related(
                "contact"
            ):
                marca = "  <<< a trilha fala por este" if pc.contact_id == contato.pk else ""
                principal = "principal" if pc.is_primary else "         "
                self.stdout.write(f"    {pc.contact.wa_id:16} {principal}{marca}")

        vinculo = PatientContact.objects.filter(contact=contato).select_related("patient").first()
        conversa = Conversation.objects.filter(clinic=clinic, contact=contato).first()

        self.stdout.write(self.style.SUCCESS(f"Ensaio ao vivo montado em {clinic.name}."))
        self.stdout.write("")
        self.stdout.write(f"  Vai receber: {contato.wa_id} ({contato.display_name or 'sem nome'})")
        if vinculo and not paciente:
            self.stdout.write(
                f"  ⚠️ Este número é da ficha de {vinculo.patient.name}. A inscrição "
                "foi feita pelo CONTATO, então a ficha dele não é tocada."
            )
        self.stdout.write(f"  Sai pelo canal: {canal.display_number}")
        self.stdout.write(
            f"  Janela de 24h: {'aberta' if conversa and conversa.window_open else 'fechada'} "
            "(por isso todo passo abre com modelo aprovado)"
        )
        self.stdout.write("")
        self.stdout.write("  As três mensagens, na ordem:")
        for nome, template in PASSOS:
            corpo = self._corpo(clinic, template)
            self.stdout.write(f"    {nome}: {corpo}")
        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                f"A trilha está DESLIGADA. Nada sai até você rodar:\n"
                f"  manage.py ensaio_ao_vivo --clinic {clinic.pk} --ligar --confirmo\n"
                f"Aí os três passos saem de {minutos} em {minutos} minutos, de verdade."
            )
        )

    def _corpo(self, clinic, template):
        t = WhatsAppTemplate.objects.filter(clinic=clinic, name=template).first()
        if t is None:
            return template
        comps = t.components if isinstance(t.components, list) else []
        for c in comps:
            if c.get("type") == "BODY":
                return (c.get("text") or "")[:80]
        return template

    def _ligar(self, clinic, minutos, *, confirmou):
        trilha = Sequence.objects.filter(clinic=clinic, name=TRILHA).first()
        if trilha is None:
            raise CommandError("O ensaio não está montado.")
        contato = self._contato(clinic)
        if not confirmou:
            raise CommandError(
                f"Isto vai mandar {len(PASSOS)} mensagens DE VERDADE para "
                f"{contato.wa_id}. Repita com --confirmo."
            )

        agora = timezone.localtime(timezone.now(), self._fuso(clinic))
        for ordem, passo in enumerate(
            SequenceStep.objects.filter(sequence=trilha).order_by("order"), start=1
        ):
            passo.send_time = (agora + timedelta(minutes=minutos * ordem)).time().replace(
                second=0, microsecond=0
            )
            passo.save(update_fields=["send_time", "updated_at"])

        trilha.is_active = True
        trilha.save(update_fields=["is_active", "updated_at"])

        for inscricao in SequenceEnrollment.objects.filter(
            sequence=trilha, status=SequenceEnrollmentStatus.ACTIVE
        ):
            recalcular(inscricao, anchor_at=timezone.now())

        self.stdout.write(self.style.SUCCESS("Trilha LIGADA. Os horários, hoje:"))
        for passo in SequenceStep.objects.filter(sequence=trilha).order_by("order"):
            self.stdout.write(f"  {passo.send_time:%H:%M}  {passo.name}")
        self.stdout.write("")
        self.stdout.write("Acompanhe com --situacao, e o painel da trilha na tela.")

    def _situacao(self, clinic):
        trilha = Sequence.objects.filter(clinic=clinic, name=TRILHA).first()
        if trilha is None:
            raise CommandError("O ensaio não está montado.")
        fuso = self._fuso(clinic)
        self.stdout.write(f"{trilha.name} · ligada={trilha.is_active}")
        for inscricao in SequenceEnrollment.objects.filter(sequence=trilha).select_related(
            "contact"
        ):
            proximo = (
                timezone.localtime(inscricao.next_dispatch_at, fuso).strftime("%H:%M")
                if inscricao.next_dispatch_at
                else "nada"
            )
            motivo = f" · motivo {inscricao.end_reason!r}" if inscricao.end_reason else ""
            segurando = f" · SEGURANDO por {inscricao.hold_reason}" if inscricao.hold_reason else ""
            self.stdout.write(
                f"  {inscricao.contact.wa_id}: {inscricao.status}{motivo}{segurando} "
                f"· próximo {proximo}"
            )
            for d in (
                SequenceDispatch.objects.filter(enrollment=inscricao)
                .select_related("step")
                .order_by("scheduled_for")
            ):
                self.stdout.write(f"      · {d.step.name}: {d.skip_reason or d.status}")

    def _zerar(self, clinic):
        SequenceDispatch.objects.filter(
            enrollment__sequence__clinic=clinic, enrollment__sequence__name=TRILHA
        ).hard_delete()
        SequenceEnrollment.objects.filter(
            sequence__clinic=clinic, sequence__name=TRILHA
        ).hard_delete()

    def _limpar(self, clinic):
        self._zerar(clinic)
        for trilha in Sequence.objects.filter(clinic=clinic, name=TRILHA):
            trilha.steps.all().hard_delete()
            trilha.hard_delete()
        Flow.objects.filter(clinic=clinic, name__startswith=f"{TRILHA}:").hard_delete()
        self.stdout.write(
            self.style.SUCCESS(
                "Ensaio ao vivo apagado. As conversas e mensagens que já saíram "
                "FICAM: elas são histórico de verdade do paciente."
            )
        )
