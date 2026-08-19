"""
Um ensaio da SEQUÊNCIA com o relógio curto, para ver em minutos o que o
calendário levaria semanas para mostrar.

    python manage.py ensaio_de_sequencia --clinic 1
    python manage.py ensaio_de_sequencia --clinic 1 --situacao
    python manage.py ensaio_de_sequencia --clinic 1 --limpar

O que ele prova é o que o painel existe para responder: **por que este
paciente não recebeu?** Cada pessoa do elenco cai numa decisão diferente do
motor, e todas na MESMA trilha, para a diferença nunca poder vir da trilha.

TRILHA A, de divulgação, três passos separados por minutos:

  · Célia    recebe os três e conclui .................. o controle
  · Marina   responde ................................... sai por "respondeu"
  · Rita     marca consulta ............................. sai por "marcou consulta"
  · Doralice tem a conversa com a recepção .............. SEGURA, e anda quando soltam
  · Neide    pediu para não receber divulgação .......... nem entra
  · Ivete    entrou com a âncora velha .................. pulada por validade
  · Zuleide  não tem número ............................. fica de fora, na conta do lote

TRILHA B, ancorada na consulta, para o que só ela faz:

  · Selma    consulta amanhã ............................ recebe o passo da VÉSPERA
  · Selma    consulta remarcada .......................... o calendário anda junto
  · Vanda    consulta cancelada .......................... sai por "consulta cancelada"
  · Lúcia    faltou ...................................... o passo de depois é pulado,
                                                            e ela CONTINUA inscrita

Os gestos: --responde, --marca-consulta, --recepcao-assume, --recepcao-solta,
--remarca, --cancela-consulta, --falta.

⚠️ **Só roda em clínica com canal FAKE**, mesma trava do `seed_sequence_demo`:
a sequência dispara fluxo, e fluxo fala com quem estiver do outro lado. Os
contatos usam o prefixo **5500** (DDD 00 não existe), então são impossíveis de
alcançar mesmo que alguém aponte isto para um canal de verdade.

⚠️ O `--responde` existe porque o webhook da Meta aponta para a PRODUÇÃO desde
18/08: resposta de paciente não chega mais na máquina local, então aqui ela é
injetada pelo mesmo caminho que a ingestão usaria.
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
from apps.automation.sequences import contato_do_paciente, inscrever, inscrever_em_lote
from apps.inbox.choices import AttendedBy, ConversationStatus, WhatsAppProviderKind
from apps.inbox.models import Channel, Conversation, WhatsAppTemplate
from apps.patients.models import Contact, Patient, PatientContact
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import Appointment, Practitioner
from apps.tenants.models import Clinic

TRILHA_A = "Ensaio A: divulgação (teste)"
TRILHA_B = "Ensaio B: da consulta (teste)"
TEMPLATE = "ensaio_teste"
PROFISSIONAL = "Dra. Ensaio (teste)"

# (chave, nome, número ou None, o que essa pessoa prova)
ELENCO_A = [
    ("celia", "Célia Ensaio", "550092000001", "recebe os três e conclui"),
    ("marina", "Marina Ensaio", "550092000002", "responde e sai"),
    ("rita", "Rita Ensaio", "550092000003", "marca consulta e sai"),
    ("doralice", "Doralice Ensaio", "550092000004", "a recepção está com a conversa"),
    ("neide", "Neide Ensaio", "550092000005", "pediu para não receber divulgação"),
    ("ivete", "Ivete Ensaio", "550092000006", "entrou com a âncora velha"),
    ("zuleide", "Zuleide Ensaio", None, "não tem número"),
]

ELENCO_B = [
    ("selma", "Selma Ensaio", "550092000007", "consulta amanhã, recebe a véspera"),
    ("vanda", "Vanda Ensaio", "550092000008", "consulta cancelada"),
    ("lucia", "Lúcia Ensaio", "550092000009", "faltou à consulta de hoje"),
]

PASSOS_A = [
    ("Primeiro convite", "Olá! Faz tempo que você não vem. Quer agendar?"),
    ("Segunda tentativa", "Passando de novo: temos horários esta semana."),
    ("Última tentativa", "Última chamada. É só responder aqui que a gente marca."),
]


def _numeros():
    return [n for _c, _nome, n, _p in ELENCO_A + ELENCO_B if n]


def _nomes():
    return [nome for _c, nome, _n, _p in ELENCO_A + ELENCO_B]


class Command(BaseCommand):
    help = "Ensaio da sequência com passos de minutos, para validar hoje."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True, help="ID da clínica")
        parser.add_argument(
            "--minutos",
            type=int,
            default=3,
            help="Intervalo entre os passos, em minutos (padrão 3).",
        )
        for flag, ajuda in [
            ("responde", "A Marina responde agora."),
            ("marca-consulta", "A Rita marca uma consulta futura."),
            ("recepcao-assume", "A recepção pega a conversa da Doralice."),
            ("recepcao-solta", "A recepção devolve a conversa da Doralice."),
            ("remarca", "A consulta da Selma muda de dia."),
            ("cancela-consulta", "A consulta da Vanda é cancelada."),
            ("falta", "A Lúcia é marcada como falta."),
            ("situacao", "Mostra o quadro inteiro."),
            ("limpar", "Apaga o ensaio."),
        ]:
            parser.add_argument(f"--{flag}", action="store_true", help=ajuda)

    def handle(self, *args, **opts):
        clinic = self._clinica(opts["clinic"])
        gestos = [
            ("limpar", self._limpar),
            ("situacao", self._situacao),
            ("responde", self._responde),
            ("marca_consulta", self._marca_consulta),
            ("recepcao_assume", lambda c: self._recepcao(c, assume=True)),
            ("recepcao_solta", lambda c: self._recepcao(c, assume=False)),
            ("remarca", self._remarca),
            ("cancela_consulta", self._cancela_consulta),
            ("falta", self._falta),
        ]
        for chave, gesto in gestos:
            if opts.get(chave):
                return gesto(clinic)

        self._montar(clinic, opts["minutos"])

    # ---- guardas e apoio ----

    def _clinica(self, pk):
        try:
            clinic = Clinic.objects.get(pk=pk)
        except Clinic.DoesNotExist:
            raise CommandError(f"Clínica {pk} não existe.")

        # ⚠️ A trava confere o canal que o DISPARO vai usar, e não "a clínica
        # tem algum canal falso". São coisas diferentes: `conversa_para_disparo`
        # pega o primeiro `is_test=False` (RF-FLW-25.5), então a clínica real
        # passaria na checagem ingênua por causa do canal do modo de teste, que
        # o motor nunca escolhe - e o ensaio sairia pelo canal de verdade.
        canal = self._canal(clinic)
        if canal is None:
            raise CommandError(
                f"A clínica {clinic.name} não tem canal de WhatsApp configurado."
            )
        if canal.provider != WhatsAppProviderKind.FAKE:
            raise CommandError(
                f"O canal que a sequência usaria na clínica {clinic.name} é "
                f"'{canal.provider}' ({canal.display_number}), e não um canal de "
                "mentira. O ensaio dispara de verdade por ele. Rode numa clínica "
                "de teste."
            )
        return clinic

    def _canal(self, clinic):
        # O MESMO que `conversa_para_disparo` escolhe, para a trava valer.
        return Channel.objects.filter(clinic=clinic, is_test=False).first()

    def _fuso(self, clinic):
        return zoneinfo.ZoneInfo(clinic.timezone or "America/Sao_Paulo")

    def _pessoa(self, clinic, chave):
        nome = next(n for c, n, _num, _p in ELENCO_A + ELENCO_B if c == chave)
        patient = Patient.objects.filter(clinic=clinic, name=nome).first()
        if patient is None:
            raise CommandError("O ensaio não está montado. Rode sem opção nenhuma antes.")
        return patient

    def _inscricao(self, clinic, patient, trilha):
        return (
            SequenceEnrollment.objects.filter(
                sequence__clinic=clinic, sequence__name=trilha, patient=patient
            )
            .order_by("-id")
            .first()
        )

    def _profissional(self, clinic):
        obj, _ = Practitioner.objects.get_or_create(clinic=clinic, name=PROFISSIONAL)
        return obj

    # ---- montar ----

    @transaction.atomic
    def _montar(self, clinic, minutos):
        if minutos < 1:
            raise CommandError("O mínimo é 1 minuto: a varredura bate a cada minuto.")

        fuso = self._fuso(clinic)
        agora = timezone.localtime(timezone.now(), fuso)
        horarios = [agora + timedelta(minutes=minutos * (i + 1)) for i in range(3)]
        if horarios[-1].date() != agora.date():
            raise CommandError(
                "O ensaio atravessaria a meia-noite. Rode mais cedo ou diminua o "
                "--minutos: o passo conta a hora do DIA da âncora."
            )

        # ⚠️ Remontar começa do ZERO. Sem isto, rodar o comando de novo por
        # cima inscreve todo mundo outra vez (a trava do banco só recusa
        # inscrição ATIVA duplicada), e o painel passa a mostrar cada pessoa
        # duas vezes, uma concluída e uma correndo. Um ensaio que mente sobre o
        # próprio estado não serve para validar nada.
        self._zerar_inscricoes(clinic)

        WhatsAppTemplate.objects.update_or_create(
            clinic=clinic,
            name=TEMPLATE,
            defaults={"category": "MARKETING", "status": "APPROVED", "language": "pt_BR"},
        )
        gente = self._criar_gente(clinic)
        trilha_a = self._trilha_a(clinic, horarios)
        trilha_b = self._trilha_b(clinic, horarios)
        contas = self._povoar_a(clinic, trilha_a, gente)
        self._povoar_b(clinic, trilha_b, gente)

        self._contar(clinic, horarios, contas)

    def _zerar_inscricoes(self, clinic):
        """Apaga inscrições e disparos do ensaio, e as consultas dos gestos."""
        SequenceDispatch.objects.filter(
            enrollment__sequence__clinic=clinic,
            enrollment__sequence__name__in=[TRILHA_A, TRILHA_B],
        ).hard_delete()
        SequenceEnrollment.objects.filter(
            sequence__clinic=clinic, sequence__name__in=[TRILHA_A, TRILHA_B]
        ).hard_delete()
        # As consultas voltam junto: a porta da consulta reinscreve sozinha, e
        # consulta velha do ensaio anterior faria o elenco nascer torto.
        Appointment.objects.filter(clinic=clinic, patient__name__in=_nomes()).hard_delete()

    def _criar_gente(self, clinic):
        gente = {}
        for chave, nome, numero, _papel in ELENCO_A + ELENCO_B:
            patient, _ = Patient.objects.get_or_create(
                clinic=clinic, name=nome, defaults={"phone": numero or ""}
            )
            gente[chave] = patient
            if numero is None:
                continue  # a Zuleide existe de propósito sem número
            contact, _ = Contact.objects.get_or_create(
                clinic=clinic, wa_id=numero, defaults={"display_name": nome}
            )
            if chave == "neide":
                contact.marketing_opt_out = True
                contact.save(update_fields=["marketing_opt_out"])
            PatientContact.objects.get_or_create(
                patient=patient, contact=contact, defaults={"is_primary": True}
            )
            if chave == "doralice":
                # A caneta precisa estar OCUPADA antes do primeiro disparo,
                # senão o motor a toma e não há o que segurar.
                Conversation.objects.get_or_create(
                    clinic=clinic,
                    contact=contact,
                    defaults={
                        "channel": self._canal(clinic),
                        "attended_by": AttendedBy.AGENT,
                        "attended_since": timezone.now(),
                        "status": ConversationStatus.OPEN,
                    },
                )
        return gente

    def _passo(self, trilha, ordem, *, nome, offset, hora, flow, validade=24):
        passo, _ = SequenceStep.objects.get_or_create(
            sequence=trilha, order=ordem, defaults={"offset_days": offset, "flow": flow}
        )
        passo.name = nome
        passo.offset_days = offset
        passo.send_time = hora.time().replace(second=0, microsecond=0)
        passo.flow = flow
        passo.expire_hours = validade
        passo.save()
        return passo

    def _fluxo(self, clinic, nome):
        anterior = Flow.objects.filter(clinic=clinic, name=nome).first()
        return criar_fluxo_de_aviso(
            clinic, nome=nome, template_name=TEMPLATE, variables={}, flow=anterior
        )

    def _trilha_a(self, clinic, horarios):
        trilha, _ = Sequence.objects.get_or_create(
            clinic=clinic,
            name=TRILHA_A,
            defaults={
                "is_active": True,
                "is_marketing": True,
                "exit_on_reply": True,
                "exit_on_appointment": True,
            },
        )
        for ordem, ((nome, _corpo), quando) in enumerate(zip(PASSOS_A, horarios), start=1):
            self._passo(
                trilha,
                ordem,
                nome=nome,
                offset=0,
                hora=quando,
                flow=self._fluxo(clinic, f"{TRILHA_A}: {nome}"),
            )
        return trilha

    def _trilha_b(self, clinic, horarios):
        trilha, _ = Sequence.objects.get_or_create(
            clinic=clinic,
            name=TRILHA_B,
            defaults={
                "is_active": True,
                "is_marketing": False,
                "enroll_on_appointment": True,
            },
        )
        # Véspera: offset NEGATIVO, o passo que só existe porque a âncora é a
        # consulta. Ele expira NA âncora, e não pelas 24h (RF-SEQ-5.2).
        self._passo(
            trilha,
            1,
            nome="Confirmação da véspera",
            offset=-1,
            hora=horarios[0],
            flow=self._fluxo(clinic, f"{TRILHA_B}: véspera"),
        )
        self._passo(
            trilha,
            2,
            nome="Como foi o atendimento",
            offset=0,
            hora=horarios[1],
            flow=self._fluxo(clinic, f"{TRILHA_B}: depois"),
        )
        return trilha

    def _povoar_a(self, clinic, trilha, gente):
        # Pelo LOTE, e não uma a uma, porque é o lote que devolve a prestação
        # de contas: quem selecionou centenas precisa saber quantos ficaram de
        # fora e por quê (RF-SEQ-9).
        pelo_lote = [gente[c] for c, _n, _num, _p in ELENCO_A if c != "ivete"]
        contas = inscrever_em_lote(trilha, pelo_lote, source=EnrollmentSource.BATCH)

        # A Ivete entra com a âncora VELHA: o primeiro passo dela já nasce
        # vencido, que é o RF-SEQ-3.5 visto de perto.
        ivete = gente["ivete"]
        inscrever(
            trilha,
            contato_do_paciente(ivete),
            source=EnrollmentSource.BATCH,
            patient=ivete,
            anchor_at=timezone.now() - timedelta(days=2),
        )
        return contas

    def _povoar_b(self, clinic, trilha, gente):
        profissional = self._profissional(clinic)
        agora = timezone.now()

        # A porta da consulta faz a inscrição sozinha (RF-SEQ-3.4), então aqui
        # só se marca a consulta e o sinal cuida do resto.
        for chave, daqui in (("selma", timedelta(days=1)), ("vanda", timedelta(days=2))):
            Appointment.objects.create(
                clinic=clinic,
                patient=gente[chave],
                practitioner=profissional,
                starts_at=agora + daqui,
            )

        # A Lúcia é o caso da falta, e a consulta dela precisa já ter
        # acontecido. Consulta no passado não passa pela porta automática (é a
        # guarda que torna o backfill barato), então ela entra à mão.
        consulta = Appointment.objects.create(
            clinic=clinic,
            patient=gente["lucia"],
            practitioner=profissional,
            starts_at=agora - timedelta(hours=6),
        )
        inscrever(
            trilha,
            contato_do_paciente(gente["lucia"]),
            source=EnrollmentSource.APPOINTMENT,
            patient=gente["lucia"],
            appointment=consulta,
            anchor_at=consulta.starts_at,
        )

    def _contar(self, clinic, horarios, contas):
        self.stdout.write(self.style.SUCCESS(f"Ensaio montado na clínica {clinic.name}."))
        self.stdout.write("")
        self.stdout.write("TRILHA A (divulgação), os passos saem hoje às:")
        for (nome, _c), quando in zip(PASSOS_A, horarios):
            self.stdout.write(f"  {quando:%H:%M}  {nome}")
        self.stdout.write(
            f"  prestação de contas do lote: {contas}"
        )
        self.stdout.write("")
        self.stdout.write("TRILHA B (ancorada na consulta):")
        self.stdout.write(f"  {horarios[0]:%H:%M}  Confirmação da véspera (D-1)")
        self.stdout.write(f"  {horarios[1]:%H:%M}  Como foi o atendimento (D+0)")
        self.stdout.write("")
        self.stdout.write(self.style.WARNING("O roteiro, na ordem:"))
        self.stdout.write(
            f"  1. Abra os painéis das duas trilhas.\n"
            f"  2. AGORA, antes do primeiro horário: --recepcao-assume.\n"
            f"  3. Às {horarios[0]:%H:%M} sai o primeiro passo. Veja a Doralice\n"
            f"     SEGURADA e a Ivete pulada por validade.\n"
            f"  4. Logo depois: --responde, --marca-consulta, --cancela-consulta,\n"
            f"     --falta e --remarca.\n"
            f"  5. --recepcao-solta faz a Doralice andar sem esperar o retry.\n"
            f"  6. Às {horarios[2]:%H:%M} a Célia recebe o último e conclui.\n"
            f"  7. --situacao a qualquer momento mostra o quadro inteiro."
        )

    # ---- os gestos ----

    def _responde(self, clinic):
        from apps.inbox.services import ingest_events
        from apps.integrations.whatsapp.events import parse_meta_webhook
        from apps.integrations.whatsapp.fake.adapter import build_inbound_payload

        patient = self._pessoa(clinic, "marina")
        inscricao = self._inscricao(clinic, patient, TRILHA_A)
        ja_recebeu = inscricao and SequenceDispatch.objects.filter(
            enrollment=inscricao
        ).exists()

        ingest_events(
            self._canal(clinic),
            parse_meta_webhook(
                build_inbound_payload(
                    wa_id=contato_do_paciente(patient).wa_id,
                    body="Quero sim, pode marcar",
                )
            ),
        )
        if inscricao:
            inscricao.refresh_from_db()

        if inscricao and inscricao.status == SequenceEnrollmentStatus.ACTIVE and not ja_recebeu:
            self.stdout.write(
                self.style.WARNING(
                    "A Marina respondeu e CONTINUA na trilha, e está certo: o "
                    "primeiro passo ainda não saiu para ela, então não havia o "
                    "que responder. Espere o primeiro horário e rode de novo."
                )
            )
            return
        self._diz("Marina", inscricao)

    def _marca_consulta(self, clinic):
        patient = self._pessoa(clinic, "rita")
        inscricao = self._inscricao(clinic, patient, TRILHA_A)
        Appointment.objects.create(
            clinic=clinic,
            patient=patient,
            practitioner=self._profissional(clinic),
            starts_at=timezone.now() + timedelta(days=3),
        )
        if inscricao:
            inscricao.refresh_from_db()
        self._diz("Rita", inscricao)

    def _recepcao(self, clinic, *, assume):
        patient = self._pessoa(clinic, "doralice")
        conversa = Conversation.objects.filter(
            clinic=clinic, contact=contato_do_paciente(patient)
        ).first()
        if conversa is None:
            raise CommandError("A Doralice não tem conversa. Monte o ensaio de novo.")

        if assume:
            conversa.attended_by = AttendedBy.AGENT
            conversa.attended_since = timezone.now()
            conversa.save(update_fields=["attended_by", "attended_since", "updated_at"])
            self.stdout.write(
                self.style.SUCCESS(
                    "A recepção está com a conversa da Doralice. O próximo passo "
                    "dela vai SEGURAR em vez de sair."
                )
            )
            return

        conversa.attended_by = AttendedBy.NONE
        conversa.attended_since = None
        conversa.save(update_fields=["attended_by", "attended_since", "updated_at"])

        # Puxa a próxima tentativa para agora. O motor empurra o relógio em 5
        # minutos a cada tentativa, e num ensaio de minutos isso quebraria o
        # ritmo. Não falseia nada: só antecipa a batida.
        inscricao = self._inscricao(clinic, patient, TRILHA_A)
        if inscricao and inscricao.status == SequenceEnrollmentStatus.ACTIVE:
            inscricao.next_dispatch_at = timezone.now()
            inscricao.save(update_fields=["next_dispatch_at", "updated_at"])
        self.stdout.write(
            self.style.SUCCESS(
                "A recepção soltou a conversa. Na próxima batida do minuto o "
                "passo segurado sai."
            )
        )

    def _remarca(self, clinic):
        patient = self._pessoa(clinic, "selma")
        inscricao = self._inscricao(clinic, patient, TRILHA_B)
        consulta = Appointment.objects.filter(clinic=clinic, patient=patient).first()
        if consulta is None or inscricao is None:
            raise CommandError("A Selma não tem consulta ou inscrição.")

        fuso = self._fuso(clinic)
        antes = inscricao.next_dispatch_at
        consulta.starts_at = consulta.starts_at + timedelta(days=2)
        consulta.save(update_fields=["starts_at", "updated_at"])
        inscricao.refresh_from_db()

        def hora(valor):
            return timezone.localtime(valor, fuso).strftime("%d/%m %H:%M") if valor else "nada"

        self.stdout.write(
            self.style.SUCCESS(
                f"A consulta da Selma foi para {hora(consulta.starts_at)}. O "
                f"próximo disparo dela era {hora(antes)} e virou "
                f"{hora(inscricao.next_dispatch_at)}."
            )
        )

    def _cancela_consulta(self, clinic):
        patient = self._pessoa(clinic, "vanda")
        inscricao = self._inscricao(clinic, patient, TRILHA_B)
        consulta = Appointment.objects.filter(clinic=clinic, patient=patient).first()
        if consulta is None:
            raise CommandError("A Vanda não tem consulta.")
        consulta.status = AppointmentStatus.CANCELED
        consulta.save(update_fields=["status", "updated_at"])
        if inscricao:
            inscricao.refresh_from_db()
        self._diz("Vanda", inscricao)

    def _falta(self, clinic):
        patient = self._pessoa(clinic, "lucia")
        consulta = Appointment.objects.filter(clinic=clinic, patient=patient).first()
        if consulta is None:
            raise CommandError("A Lúcia não tem consulta.")
        consulta.status = AppointmentStatus.NO_SHOW
        consulta.save(update_fields=["status", "updated_at"])
        inscricao = self._inscricao(clinic, patient, TRILHA_B)
        self.stdout.write(
            self.style.SUCCESS(
                "A Lúcia está marcada como falta. O passo de DEPOIS da consulta "
                "vai ser pulado, e ela continua inscrita: faltar não é cancelar."
            )
        )
        self._diz("Lúcia", inscricao)

    def _diz(self, quem, inscricao):
        if inscricao is None:
            self.stdout.write(f"  {quem}: sem inscrição.")
            return
        motivo = f" · motivo {inscricao.end_reason!r}" if inscricao.end_reason else ""
        self.stdout.write(self.style.SUCCESS(f"  {quem}: {inscricao.status}{motivo}"))

    # ---- olhar ----

    def _situacao(self, clinic):
        fuso = self._fuso(clinic)
        for trilha_nome, elenco in ((TRILHA_A, ELENCO_A), (TRILHA_B, ELENCO_B)):
            trilha = Sequence.objects.filter(clinic=clinic, name=trilha_nome).first()
            if trilha is None:
                raise CommandError("O ensaio não está montado.")
            self.stdout.write("")
            self.stdout.write(self.style.WARNING(trilha.name))
            for chave, nome, _numero, papel in elenco:
                patient = Patient.objects.filter(clinic=clinic, name=nome).first()
                inscricao = self._inscricao(clinic, patient, trilha_nome) if patient else None
                if inscricao is None:
                    self.stdout.write(f"  {nome:18} fora da trilha ({papel})")
                    continue
                self.stdout.write(f"  {nome:18} {inscricao.status}{self._detalhe(inscricao, fuso)}")
                for d in SequenceDispatch.objects.filter(
                    enrollment=inscricao
                ).select_related("step").order_by("scheduled_for"):
                    marca = d.skip_reason or d.status
                    self.stdout.write(f"       · {d.step.name}: {marca}")

    def _detalhe(self, inscricao, fuso):
        partes = []
        if inscricao.end_reason:
            partes.append(f"motivo {inscricao.end_reason!r}")
        if inscricao.hold_reason:
            desde = timezone.localtime(inscricao.held_since, fuso) if inscricao.held_since else None
            partes.append(
                f"SEGURANDO por {inscricao.hold_reason}"
                + (f" desde {desde:%H:%M}" if desde else "")
            )
        if inscricao.next_dispatch_at:
            quando = timezone.localtime(inscricao.next_dispatch_at, fuso)
            partes.append(f"próximo {quando:%d/%m %H:%M}")
        return (" · " + " · ".join(partes)) if partes else ""

    # ---- limpar ----

    def _limpar(self, clinic):
        from apps.automation.models import FlowRun, FlowRunEvent
        from apps.inbox.models import Message

        SequenceDispatch.objects.filter(
            enrollment__sequence__clinic=clinic,
            enrollment__sequence__name__in=[TRILHA_A, TRILHA_B],
        ).hard_delete()
        for trilha in Sequence.objects.filter(clinic=clinic, name__in=[TRILHA_A, TRILHA_B]):
            trilha.enrollments.all().hard_delete()
            trilha.steps.all().hard_delete()
            trilha.hard_delete()

        Appointment.objects.filter(clinic=clinic, patient__name__in=_nomes()).hard_delete()
        Practitioner.objects.filter(clinic=clinic, name=PROFISSIONAL).hard_delete()

        conversas = Conversation.objects.filter(clinic=clinic, contact__wa_id__in=_numeros())
        runs = FlowRun.objects.filter(conversation__in=conversas)
        FlowRunEvent.objects.filter(run__in=runs).hard_delete()
        runs.hard_delete()
        Message.objects.filter(conversation__in=conversas).hard_delete()
        conversas.hard_delete()

        Flow.objects.filter(
            clinic=clinic, name__startswith="Ensaio "
        ).hard_delete()
        for patient in Patient.objects.filter(clinic=clinic, name__in=_nomes()):
            PatientContact.objects.filter(patient=patient).hard_delete()
            patient.hard_delete()
        Contact.objects.filter(clinic=clinic, wa_id__in=_numeros()).hard_delete()
        WhatsAppTemplate.objects.filter(clinic=clinic, name=TEMPLATE).hard_delete()

        self.stdout.write(self.style.SUCCESS("Ensaio apagado."))
