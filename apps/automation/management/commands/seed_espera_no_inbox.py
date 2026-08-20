"""
Põe na tela os três estados da faixa de sequência segurada (RF-SEQ-5.5).

    python manage.py seed_espera_no_inbox --clinic 1
    python manage.py seed_espera_no_inbox --clinic 1 --limpar

Monta três conversas lado a lado no Inbox, para comparar de uma olhada só:

  1. **Uma sequência esperando** - a faixa diz o nome e desde quando.
  2. **Duas esperando** - o nome sai da frase e entra a contagem, com o tempo
     da mais antiga.
  3. **Nada esperando** - o cabeçalho de sempre, sem faixa e sem espaço
     reservado.

⚠️ **Só roda em clínica com canal FAKE.** Nada aqui manda mensagem, mas a
conversa fica ATENDIDA para o motor segurar de verdade, e uma clínica real não
é lugar de conversa inventada. Os contatos usam o prefixo **5500** (o DDD 00
não existe), o mesmo do `seed_inbox_demo`, e `--limpar` apaga exatamente esse
recorte.
"""

from datetime import time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.automation.choices import (
    EnrollmentSource,
    FlowStatus,
    FlowTrigger,
    HoldReason,
    SequenceEnrollmentStatus,
)
from apps.automation.models import (
    Flow,
    FlowVersion,
    Sequence,
    SequenceEnrollment,
    SequenceStep,
)
from apps.inbox.choices import AttendedBy, ConversationStatus
from apps.inbox.models import Channel, Conversation
from apps.patients.models import Contact
from apps.tenants.models import Clinic

PREFIXO = "5500"

PESSOAS = [
    ("55009000101", "Willian Souza", ["Pré-consulta"], 2),
    ("55009000102", "Maria Alves", ["Retorno em 6 meses", "Pesquisa de satisfação"], 72),
    ("55009000103", "João Pedro", [], 0),
]


class Command(BaseCommand):
    help = "Semeia conversas segurando sequência, para ver a faixa do RF-SEQ-5.5."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, default=1)
        parser.add_argument("--limpar", action="store_true")
        parser.add_argument(
            "--confirmo",
            action="store_true",
            help="Exigido em clínica com canal de VERDADE (a MedEssence).",
        )

    @transaction.atomic
    def handle(self, *args, **opts):
        clinic = Clinic.objects.filter(pk=opts["clinic"]).first()
        if clinic is None:
            raise CommandError(f"Clínica {opts['clinic']} não existe.")

        channel = Channel.objects.filter(clinic=clinic).first()
        if channel is None:
            raise CommandError("Esta clínica não tem canal de WhatsApp.")
        # ⚠️ A trava olha o canal que a conversa USARIA, não "a clínica tem
        # algum canal fake": a clínica real TEM um fake, o do modo de teste, e
        # o disparo nunca escolhe aquele.
        # ⚠️ LIMPAR vem ANTES da trava, e a ordem importa. A trava existe para
        # proteger contra ESCREVER lixo no Inbox de quem trabalha; exigir
        # `--confirmo` para REMOVER esse mesmo lixo é pedir "sim, quero criar
        # conversa falsa" a quem está tentando desfazer. Estava assim, e foi o
        # que atrapalhou a limpeza da clínica 3 em 20/08/2026.
        if opts["limpar"]:
            return self._limpar(clinic)

        if not self._e_fake(channel) and not opts["confirmo"]:
            raise CommandError(
                f"O canal da clínica {clinic.pk} ({channel.display_number}) é de "
                "VERDADE. Este comando inventa conversa atendida no Inbox de "
                "quem trabalha ali.\n"
                "Se é isso mesmo, repita com --confirmo. Os números usados são "
                f"do prefixo {PREFIXO} (DDD 00 não existe, ninguém recebe nada) "
                "e a inscrição nasce impossível de disparar. Para desfazer: "
                "--limpar, que não pede confirmação nenhuma."
            )

        for wa_id, nome, trilhas, horas in PESSOAS:
            conversa = self._conversa(clinic, channel, wa_id, nome, atendida=bool(trilhas))
            for i, trilha in enumerate(trilhas):
                self._segurar(clinic, conversa, trilha, horas=horas - i * 1)
            self.stdout.write(
                f"  {nome}: {len(trilhas)} esperando"
                + (f" (a mais antiga há {horas}h)" if trilhas else "")
            )

        self.stdout.write(
            self.style.SUCCESS(
                "\nPronto. Abra o Inbox e compare as três conversas do topo.\n"
                "Para desfazer: --limpar"
            )
        )

    def _e_fake(self, channel) -> bool:
        from apps.inbox.choices import WhatsAppProviderKind

        return channel.provider == WhatsAppProviderKind.FAKE

    def _conversa(self, clinic, channel, wa_id, nome, *, atendida):
        contact, _ = Contact.objects.get_or_create(
            clinic=clinic, wa_id=wa_id, defaults={"display_name": nome}
        )
        conversa, _ = Conversation.objects.get_or_create(
            clinic=clinic,
            channel=channel,
            contact=contact,
            defaults={"status": ConversationStatus.OPEN},
        )
        conversa.status = ConversationStatus.OPEN
        # Atendida é o que faz o motor segurar: sem dono, ele tomaria a caneta
        # e a mensagem sairia.
        conversa.attended_by = AttendedBy.AGENT if atendida else AttendedBy.NONE
        conversa.attended_since = timezone.now() if atendida else None
        conversa.last_message_preview = f"Conversa de exemplo com {nome}"
        conversa.last_message_at = timezone.now()
        conversa.last_inbound_at = timezone.now()
        conversa.save()
        return conversa

    def _segurar(self, clinic, conversa, nome_da_trilha, *, horas):
        sequence = self._trilha(clinic, nome_da_trilha)
        inscricao, _ = SequenceEnrollment.objects.get_or_create(
            clinic=clinic,
            sequence=sequence,
            contact=conversa.contact,
            defaults={
                "status": SequenceEnrollmentStatus.ACTIVE,
                "source": EnrollmentSource.PATIENT_RECORD,
                "anchor_at": timezone.now(),
            },
        )
        inscricao.status = SequenceEnrollmentStatus.ACTIVE
        inscricao.hold_reason = HoldReason.BUSY
        inscricao.held_since = timezone.now() - timedelta(hours=max(horas, 1))
        # ⚠️ Impossível de disparar, DE PROPÓSITO. A conversa está atendida, e é
        # por isso que o motor segura; mas se alguém encerrar o atendimento na
        # clínica REAL, a varredura tentaria falar com este número. Empurrar o
        # relógio para daqui a dez anos tira essa chance: o exemplo serve para
        # OLHAR, não para disparar.
        inscricao.next_dispatch_at = timezone.now() + timedelta(days=3650)
        inscricao.save()
        return inscricao

    def _trilha(self, clinic, nome):
        sequence = Sequence.objects.filter(clinic=clinic, name=nome).first()
        if sequence is None:
            sequence = Sequence.objects.create(clinic=clinic, name=nome, is_active=True)
        if not sequence.steps.exists():
            SequenceStep.objects.create(
                sequence=sequence,
                order=1,
                offset_days=1,
                send_time=time(8, 0),
                flow=self._fluxo(clinic, nome),
            )
        return sequence

    def _fluxo(self, clinic, nome):
        flow = Flow.objects.filter(clinic=clinic, name=f"Aviso de {nome}").first()
        if flow:
            return flow
        flow = Flow.objects.create(
            clinic=clinic,
            name=f"Aviso de {nome}",
            status=FlowStatus.ACTIVE,
            trigger=FlowTrigger.MANUAL,
        )
        version = FlowVersion.objects.create(
            flow=flow, number=1, graph={"nodes": [], "edges": [], "entry_node": ""}
        )
        flow.current_version = version
        flow.save(update_fields=["current_version"])
        return flow

    def _limpar(self, clinic):
        """
        Desfaz o que este comando semeou, e SÓ isso.

        ⚠️ Duas correções de 20/08/2026, as duas achadas com o estrago já
        feito na clínica REAL:

        1. **As CONVERSAS não eram removidas.** O comando prometia "para
           desfazer: --limpar" e deixava para trás três conversas de exemplo
           na fila da recepção, com `attended_by=AGENT` sem dono (que é o que
           ele usa para demonstrar a sequência segurada). Elas ficaram no
           Inbox da clínica 3 por um dia, e o usuário as leu como defeito do
           produto: "conversa em sequência entra como Atendendo".

        2. ⚠️ **Apagava SEQUÊNCIA E FLUXO POR NOME.** "Pré-consulta",
           "Retorno em 6 meses" e "Pesquisa de satisfação" são nomes que
           qualquer clínica usa de verdade - a 3 tinha as três. Rodar
           `--limpar` lá apagaria trilha da clínica que este comando nunca
           criou. Isso saiu: o comando não tem como distinguir a que ele fez
           da que já existia, e na dúvida não apaga.

        O que ele apaga agora é só o que carrega a marca dele: os contatos dos
        números de `PESSOAS`, a conversa de cada um e a inscrição deles.
        Trilha e fluxo ficam, e o comando DIZ que ficaram.
        """
        # ⚠️ Pelos NÚMEROS de `PESSOAS`, e não pelo prefixo `5500`: o prefixo
        # é compartilhado com outros seeds (o `seed_inbox_demo` tem oito
        # contatos nele), e um comando que desfaz o dos outros é pior do que
        # um que não desfaz o próprio. Medido na clínica 3: o prefixo pegava
        # 11 contatos, e só 3 são deste comando.
        numeros = [wa_id for wa_id, *_ in PESSOAS]
        contatos = Contact.objects.filter(clinic=clinic, wa_id__in=numeros)
        quantos = contatos.count()
        if not quantos:
            self.stdout.write("Nada deste comando por aqui.")
            return

        SequenceEnrollment.objects.filter(clinic=clinic, contact__in=contatos).delete()
        # `all_objects` e `hard_delete`: é lixo de semente, não histórico de
        # atendimento. Apagado de leve, ele continuaria ocupando a unicidade
        # do número e reaparecendo em consulta que use `all_objects`.
        for conversa in Conversation.all_objects.filter(clinic=clinic, contact__in=contatos):
            conversa.hard_delete()
        for contato in contatos:
            contato.hard_delete()

        self.stdout.write(
            self.style.SUCCESS(f"Removidos {quantos} contatos de exemplo, com as conversas deles.")
        )
        nomes = ", ".join(sorted({t for _, _, trilhas, _ in PESSOAS for t in trilhas}))
        self.stdout.write(
            f"⚠️ As trilhas ({nomes}) e os fluxos de aviso NÃO foram tocados: "
            "o nome delas é comum e podem ser da clínica. Confira e apague à mão "
            "se forem deste comando."
        )
