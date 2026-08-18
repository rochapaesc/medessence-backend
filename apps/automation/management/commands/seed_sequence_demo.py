"""
Semeia uma SEQUÊNCIA de demonstração (F3, §4.4) e permite disparar na hora.

    python manage.py seed_sequence_demo --clinic 1
    python manage.py seed_sequence_demo --clinic 1 --disparar
    python manage.py seed_sequence_demo --clinic 1 --limpar

⚠️ **Só roda em clínica com canal FAKE.** A sequência dispara fluxo, que manda
mensagem: apontá-la para um canal de verdade falaria com paciente de verdade
sem ninguém pedir. O guarda está aqui e não na cabeça de quem digita.

Os contatos de teste usam o prefixo **5500** (DDD 00 não existe, então o número
é impossível de receber mensagem), o mesmo do `seed_inbox_demo`, e `--limpar`
apaga exatamente esse prefixo.

A trilha semeada tem dois passos e serve para VER as três respostas do motor:

  1. **Aviso de retorno** (hoje, 08:00) - abre com MODELO aprovado, então sai
     mesmo com a janela de 24h fechada, que é o caso normal de uma sequência.
  2. **Pesquisa de satisfação** (amanhã, 09:00) - abre com TEXTO, então só sai
     se o paciente tiver falado nas últimas 24h. É o passo que demonstra o
     adiamento por janela.
"""

import zoneinfo
from datetime import time, timedelta

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.db.models import Q
from django.utils import timezone

from apps.automation.choices import (
    EnrollmentSource,
    FlowNodeType,
    FlowStatus,
    FlowTrigger,
    SequenceEnrollmentStatus,
)
from apps.automation.models import Flow, FlowVersion, Sequence, SequenceEnrollment, SequenceStep
from apps.automation.sequences import inscrever
from apps.inbox.choices import WhatsAppProviderKind
from apps.inbox.models import Channel, WhatsAppTemplate
from apps.patients.models import Contact, Patient, PatientContact
from apps.tenants.models import Clinic

SEQUENCIA = "Pós-consulta (teste)"
JORNADA = "Jornada da consulta (teste)"
RESGATE = "Resgate (teste)"
TEMPLATE = "retorno_teste"
# Cidade inventada, para o recorte do teste não pegar paciente de verdade.
CIDADE_TESTE = "Vila Teste"
PREFIXO = "5500"
PACIENTES = [
    ("Ana Teste", "550091000001"),
    ("Bruno Teste", "550091000002"),
]


def _fluxo(clinic, nome, nodes, edges):
    flow, _ = Flow.objects.get_or_create(
        clinic=clinic,
        name=nome,
        defaults={"trigger": FlowTrigger.MANUAL, "priority": 50},
    )
    ultima = flow.versions.order_by("-number").first()
    version = FlowVersion.objects.create(
        flow=flow,
        number=(ultima.number if ultima else 0) + 1,
        graph={"entry_node": "n1", "nodes": nodes, "edges": edges},
        published_at=timezone.now(),
    )
    flow.current_version = version
    flow.status = FlowStatus.ACTIVE
    flow.activated_at = timezone.now()
    flow.save(update_fields=["current_version", "status", "activated_at", "updated_at"])
    return flow


class Command(BaseCommand):
    help = "Semeia uma sequência de teste (F3) e, com --disparar, resolve o passo vencido."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True, help="ID da clínica")
        parser.add_argument(
            "--disparar",
            action="store_true",
            help="Vence o primeiro passo e resolve o disparo na hora.",
        )
        parser.add_argument(
            "--consulta",
            action="store_true",
            help="Marca uma consulta futura e exercita a porta automática (RF-SEQ-3.4).",
        )
        parser.add_argument(
            "--lote",
            type=int,
            default=0,
            metavar="N",
            help="Cria N pacientes de teste e inscreve a seleção em lote (RF-SEQ-3.3).",
        )
        parser.add_argument(
            "--recorte",
            type=int,
            default=0,
            metavar="N",
            help=(
                "Cria N pacientes numa cidade de teste e inscreve O RECORTE pelo "
                "endpoint, como faz a tela quando marca a fila inteira (RF-REA-2.5)."
            ),
        )
        parser.add_argument(
            "--teto",
            type=int,
            default=0,
            metavar="N",
            help="Baixa o teto do lote só nesta execução, para ver o corte parcial.",
        )
        parser.add_argument("--limpar", action="store_true", help="Remove o que foi semeado")

    @transaction.atomic
    def handle(self, *args, **options):
        clinic = Clinic.objects.filter(pk=options["clinic"]).first()
        if not clinic:
            raise CommandError(f"Clínica {options['clinic']} não encontrada.")

        canal = Channel.objects.filter(clinic=clinic).first()
        if canal is None:
            raise CommandError(f"A clínica {clinic.name} não tem canal de WhatsApp.")
        if canal.provider != WhatsAppProviderKind.FAKE:
            raise CommandError(
                f"A clínica {clinic.name} usa o canal '{canal.provider}', que fala com o "
                f"WhatsApp de verdade. Este comando só roda em canal FAKE - semear "
                f"sequência aqui mandaria mensagem para paciente de verdade."
            )

        if options["limpar"]:
            return self._limpar(clinic)

        trilha = self._semear(clinic)
        if options["lote"]:
            self._lote(clinic, trilha, options["lote"])
        if options["recorte"]:
            self._recorte(clinic, options["recorte"], options["teto"])
        if options["consulta"]:
            self._consulta(clinic)
        if options["disparar"]:
            self._disparar(clinic, trilha)

    # ---- semeadura ----

    def _semear(self, clinic):
        WhatsAppTemplate.objects.get_or_create(
            clinic=clinic,
            name=TEMPLATE,
            defaults={
                "language": "pt_BR",
                "category": "UTILITY",
                "status": "APPROVED",
                "components": [
                    {"type": "BODY", "text": "Olá, {{1}}! Já faz um tempo desde a sua consulta."}
                ],
            },
        )

        aviso = _fluxo(
            clinic,
            "Sequência: aviso de retorno (teste)",
            [
                {
                    "id": "n1",
                    "type": FlowNodeType.SEND_TEMPLATE,
                    "label": "Aviso de retorno",
                    "config": {
                        "template_name": TEMPLATE,
                        "variables": {"1": {"source": "patient_first_name"}},
                    },
                },
                {
                    "id": "n2",
                    "type": FlowNodeType.SEND_BUTTONS,
                    "label": "Quer marcar?",
                    "config": {
                        "text": "Quer que eu já veja um horário para você?",
                        "buttons": [
                            {"id": "sim", "title": "Quero marcar"},
                            {"id": "nao", "title": "Agora não"},
                        ],
                    },
                },
            ],
            [{"from": "n1", "to": "n2", "condition": "default"}],
        )

        pesquisa = _fluxo(
            clinic,
            "Sequência: pesquisa (teste)",
            [
                {
                    "id": "n1",
                    "type": FlowNodeType.SEND_MESSAGE,
                    "label": "Pesquisa",
                    "config": {"text": "De 0 a 10, como foi o seu atendimento?"},
                }
            ],
            [],
        )

        trilha, criada = Sequence.objects.get_or_create(
            clinic=clinic,
            name=SEQUENCIA,
            defaults={"is_active": True, "is_marketing": False},
        )
        # `update_or_create` com defaults desfaria em silêncio o que a tela
        # mudou (foi o que mordeu no `seed_flow_clinica`): semear de novo
        # atualiza o DESENHO dos passos e não mexe na política da sequência.
        for ordem, (nome, offset, hora, flow, validade) in enumerate(
            [
                ("Aviso de retorno", 0, time(8, 0), aviso, 24),
                ("Pesquisa de satisfação", 1, time(9, 0), pesquisa, 12),
            ],
            start=1,
        ):
            passo, _ = SequenceStep.objects.get_or_create(
                sequence=trilha, order=ordem, defaults={"offset_days": offset, "flow": flow}
            )
            passo.name = nome
            passo.offset_days = offset
            passo.send_time = hora
            passo.flow = flow
            passo.expire_hours = validade
            passo.save()

        inscritos = 0
        for nome, numero in PACIENTES:
            patient, _ = Patient.objects.get_or_create(
                clinic=clinic, name=nome, defaults={"phone": numero}
            )
            contact, _ = Contact.objects.get_or_create(
                clinic=clinic, wa_id=numero, defaults={"display_name": nome}
            )
            PatientContact.objects.get_or_create(
                patient=patient, contact=contact, defaults={"is_primary": True}
            )
            if inscrever(trilha, contact, source=EnrollmentSource.PATIENT_RECORD, patient=patient):
                inscritos += 1

        verbo = "Criada" if criada else "Atualizada"
        self.stdout.write(
            self.style.SUCCESS(
                f"{verbo} a sequência '{SEQUENCIA}' na clínica {clinic.name} "
                f"(2 passos, {inscritos} inscrição(ões) nova(s))."
            )
        )
        for enrollment in SequenceEnrollment.objects.filter(
            sequence=trilha, status=SequenceEnrollmentStatus.ACTIVE
        ).select_related("contact", "current_step"):
            quando = timezone.localtime(
                enrollment.next_dispatch_at,
                zoneinfo.ZoneInfo(clinic.timezone or "America/Sao_Paulo"),
            )
            self.stdout.write(
                f"  {enrollment.contact.wa_id} · {enrollment.current_step} · "
                f"previsto para {quando:%d/%m %H:%M}"
            )
        return trilha

    # ---- lote da fila de resgate (RF-SEQ-3.3) ----

    def _lote(self, clinic, trilha_operacional, quantos):
        """
        Monta uma seleção parecida com a da fila de resgate real e inscreve.

        Dois dos pacientes nascem "problemáticos" de propósito, porque o que se
        quer VER é a prestação de contas: quem selecionou centenas precisa
        saber quantos ficaram de fora e por quê.

        ⚠️ Trilha PRÓPRIA e de marketing, não a `trilha_operacional` do resto
        da demonstração: resgate é `MARKETING` na Meta (§4.5), e é justamente
        isso que faz o opt-out valer. Com a operacional, o paciente que pediu
        silêncio entraria - e a demonstração esconderia a regra que é obrigação
        legal.
        """
        from apps.automation.sequences import inscrever_em_lote

        trilha, criada = Sequence.objects.get_or_create(
            clinic=clinic,
            name=RESGATE,
            defaults={"is_active": True, "is_marketing": True},
        )
        if criada:
            SequenceStep.objects.create(
                sequence=trilha,
                order=1,
                name="Convite de volta",
                offset_days=0,
                send_time=time(9, 0),
                flow=Flow.objects.get(
                    clinic=clinic, name="Sequência: aviso de retorno (teste)"
                ),
            )

        selecao = []
        for i in range(quantos):
            nome = f"Resgate {i + 1:03d}"
            patient, _ = Patient.objects.get_or_create(clinic=clinic, name=nome)
            selecao.append(patient)

            if i == 0:
                continue  # o primeiro fica SEM número, de propósito

            contact, _ = Contact.objects.get_or_create(
                clinic=clinic,
                wa_id=f"5500{92000000 + i}",
                defaults={"display_name": nome},
            )
            PatientContact.objects.get_or_create(
                patient=patient, contact=contact, defaults={"is_primary": True}
            )
            if i == 1:
                contact.marketing_opt_out = True  # o segundo pediu silêncio
                contact.save(update_fields=["marketing_opt_out"])

        contas = inscrever_em_lote(trilha, selecao)

        self.stdout.write("")
        self.stdout.write(f"Lote de {quantos} na trilha '{trilha.name}':")
        for chave, rotulo in [
            ("inscritos", "inscritos"),
            ("sem_numero", "sem número de WhatsApp"),
            ("opt_out", "pediram para não receber"),
            ("ja_inscritos", "já estavam na trilha"),
        ]:
            self.stdout.write(f"    {contas[chave]:>4}  {rotulo}")

        tz = zoneinfo.ZoneInfo(clinic.timezone or "America/Sao_Paulo")
        levas = {}
        for quando in SequenceEnrollment.objects.filter(
            sequence=trilha, source="batch"
        ).values_list("next_dispatch_at", flat=True):
            chave = f"{timezone.localtime(quando, tz):%d/%m %H:%M}"
            levas[chave] = levas.get(chave, 0) + 1
        self.stdout.write("  disparos deslizados em levas:")
        for quando, quantos_na_leva in sorted(levas.items()):
            self.stdout.write(f"    {quando} · {quantos_na_leva} disparo(s)")

    # ---- o recorte inteiro, pelo endpoint (RF-REA-2.5) ----

    def _recorte(self, clinic, quantos, teto):
        """
        Inscreve O RECORTE, e não uma lista de ids.

        ⚠️ Passa pelo **endpoint**, não pelo `inscrever_em_lote`: o que muda
        aqui é o servidor expandir o filtro pelo mesmo `PatientFilterset` da
        listagem, e chamar o serviço direto pularia exatamente a parte nova.

        Os pacientes nascem com `last_appointment_at` escalonado para dar para
        conferir que, quando não cabe todo mundo, entram os que sumiram há
        mais tempo.
        """
        from rest_framework.test import APIClient

        from apps.accounts.choices import MembershipRole
        from apps.accounts.models import Membership
        from apps.automation.api.viewsets import sequence as viewset

        trilha = Sequence.objects.filter(clinic=clinic, name=RESGATE).first()
        if trilha is None:
            raise CommandError("Rode antes com --lote para criar a trilha de resgate.")

        gestor = (
            Membership.objects.filter(clinic=clinic, role=MembershipRole.MANAGER)
            .select_related("user")
            .first()
        )
        if gestor is None:
            raise CommandError(f"A clínica {clinic.pk} não tem gestor para autenticar.")

        agora = timezone.now()
        for i in range(quantos):
            nome = f"Recorte {i + 1:03d}"
            patient, _ = Patient.objects.get_or_create(
                clinic=clinic,
                name=nome,
                defaults={"city": CIDADE_TESTE},
            )
            # Quanto maior o índice, há mais tempo sumiu: o último da lista é o
            # primeiro a entrar quando o lote não comporta todos.
            Patient.objects.filter(pk=patient.pk).update(
                city=CIDADE_TESTE, last_appointment_at=agora - timedelta(days=30 * (i + 1))
            )
            contact, _ = Contact.objects.get_or_create(
                clinic=clinic,
                wa_id=f"5500{93000000 + i}",
                defaults={"display_name": nome},
            )
            PatientContact.objects.get_or_create(
                patient=patient, contact=contact, defaults={"is_primary": True}
            )

        # Um desmarcado à mão, que é o que a tela manda em `excluir`.
        desmarcado = Patient.objects.get(clinic=clinic, name="Recorte 001")

        original = viewset.MAX_POR_LOTE
        if teto:
            viewset.MAX_POR_LOTE = teto
            self.stdout.write(
                self.style.WARNING(f"  teto do lote baixado para {teto} só nesta execução")
            )
        try:
            client = APIClient()
            client.force_authenticate(gestor.user)
            resposta = client.post(
                f"/api/v1/sequences/{trilha.pk}/enroll-batch/",
                {"filtros": {"city": CIDADE_TESTE}, "excluir": [desmarcado.pk]},
                format="json",
                # Fora do pytest, `testserver` não está em ALLOWED_HOSTS.
                SERVER_NAME="localhost",
                # O gestor da demonstração costuma ter mais de uma clínica, e
                # aí o escopo é obrigatório, igual ao que o app manda.
                HTTP_X_CLINIC_ID=str(clinic.pk),
            )
        finally:
            viewset.MAX_POR_LOTE = original

        self.stdout.write("")
        self.stdout.write(
            f"Recorte de {quantos} em '{CIDADE_TESTE}' (1 desmarcado) "
            f"na trilha '{trilha.name}':"
        )
        self.stdout.write(f"  HTTP {resposta.status_code}")
        if resposta.status_code != 201:
            corpo = getattr(resposta, "data", None) or resposta.content[:400]
            self.stdout.write(self.style.ERROR(f"  {corpo}"))
            return
        for chave, rotulo in [
            ("inscritos", "inscritos"),
            ("ja_inscritos", "já estavam dentro"),
            ("sem_numero", "sem número de WhatsApp"),
            ("opt_out", "pediram para não receber"),
            ("fora_do_lote", "ficaram para a próxima leva"),
        ]:
            self.stdout.write(f"  {resposta.data.get(chave, 0)} {rotulo}")

        entraram = (
            SequenceEnrollment.objects.filter(
                sequence=trilha,
                status=SequenceEnrollmentStatus.ACTIVE,
                contact__wa_id__startswith="550093",
            )
            .order_by("next_dispatch_at")
            .values_list("contact__display_name", flat=True)
        )
        self.stdout.write(f"  ordem de entrada: {', '.join(entraram) or 'ninguém'}")

    # ---- porta automática da consulta (RF-SEQ-3.4) ----

    def _consulta(self, clinic):
        """
        Marca uma consulta futura para a Ana e mostra a jornada nascendo,
        reancorando na remarcação e morrendo com o cancelamento.

        Não chama nada do motor de sequências: quem faz tudo é o `post_save`
        da consulta, que é justamente o que se quer ver.
        """
        from apps.scheduling.choices import AppointmentStatus
        from apps.scheduling.models import Appointment, Practitioner

        trilha, criada = Sequence.objects.get_or_create(
            clinic=clinic,
            name=JORNADA,
            defaults={
                "is_active": True,
                "is_marketing": False,
                "enroll_on_appointment": True,
            },
        )
        if criada:
            aviso = Flow.objects.filter(
                clinic=clinic, name="Sequência: aviso de retorno (teste)"
            ).first()
            for ordem, (nome, offset, hora) in enumerate(
                [("Confirmação da véspera", -1, time(8, 0)), ("Como foi?", 1, time(18, 0))],
                start=1,
            ):
                SequenceStep.objects.create(
                    sequence=trilha,
                    order=ordem,
                    name=nome,
                    offset_days=offset,
                    send_time=hora,
                    flow=aviso,
                )

        patient = Patient.objects.filter(clinic=clinic, name=PACIENTES[0][0]).first()
        profissional, _ = Practitioner.objects.get_or_create(clinic=clinic, name="Dra. Teste")

        self.stdout.write("")
        self.stdout.write(f"Jornada '{JORNADA}' (D-1 e D+1), pela porta automática:")

        # Duas consultas, porque são dois arcos diferentes e uma só faria a
        # demonstração mentir: depois do pulo do último passo a trilha CONCLUI,
        # e aí o cancelamento não teria mais o que cancelar.
        primeira = Appointment.objects.create(
            clinic=clinic,
            patient=patient,
            practitioner=profissional,
            starts_at=timezone.now() + timedelta(days=10),
            status=AppointmentStatus.SCHEDULED,
        )
        self._mostrar(clinic, primeira, "consulta marcada para daqui a 10 dias")

        primeira.starts_at = primeira.starts_at + timedelta(days=5)
        primeira.save()
        self._mostrar(clinic, primeira, "data empurrada em 5 dias (reancora)")

        # A falta NÃO encerra a trilha (RF-SEQ-7.1): pula só os passos que caem
        # depois da consulta, e é preciso resolver um para ver o motivo.
        primeira.status = AppointmentStatus.NO_SHOW
        primeira.save()
        self._faltou(clinic, primeira)

        segunda = Appointment.objects.create(
            clinic=clinic,
            patient=patient,
            practitioner=profissional,
            starts_at=timezone.now() + timedelta(days=30),
            status=AppointmentStatus.SCHEDULED,
        )
        self._mostrar(clinic, segunda, "outra consulta, daqui a 30 dias")
        segunda.status = AppointmentStatus.CANCELED
        segunda.save()
        self._mostrar(clinic, segunda, "essa foi cancelada")

    def _faltou(self, clinic, consulta):
        """Vence o passo pós-consulta e mostra o motivo do pulo."""
        from apps.automation.sequences import horario_do_passo, resolver_disparo

        inscricao = SequenceEnrollment.objects.filter(appointment=consulta).first()
        if inscricao is None:
            return
        pos = inscricao.sequence.steps.filter(offset_days__gt=0).order_by("order").first()
        inscricao.current_step = pos
        inscricao.next_dispatch_at = horario_do_passo(pos, inscricao.anchor_at, clinic)
        inscricao.save(update_fields=["current_step", "next_dispatch_at"])
        SequenceEnrollment.objects.filter(pk=inscricao.pk).update(
            next_dispatch_at=timezone.now() - timedelta(minutes=1)
        )

        resultado = resolver_disparo(inscricao.pk)
        disparo = inscricao.dispatches.order_by("-pk").first()
        self.stdout.write("  paciente faltou, e o passo de DEPOIS venceu:")
        self.stdout.write(
            f"    {resultado} · motivo gravado={disparo.skip_reason if disparo else chr(45)}"
        )

    def _mostrar(self, clinic, consulta, titulo):
        tz = zoneinfo.ZoneInfo(clinic.timezone or "America/Sao_Paulo")
        inscricao = SequenceEnrollment.objects.filter(appointment=consulta).first()
        self.stdout.write(f"  {titulo}:")
        if inscricao is None:
            self.stdout.write("    (nenhuma inscrição)")
            return
        quando = (
            f"{timezone.localtime(inscricao.next_dispatch_at, tz):%d/%m %H:%M}"
            if inscricao.next_dispatch_at
            else "-"
        )
        self.stdout.write(
            f"    {inscricao.status}"
            f"{' (' + inscricao.end_reason + ')' if inscricao.end_reason else ''}"
            f" · passo={inscricao.current_step} · próximo disparo={quando}"
        )

    # ---- disparo ----

    def _disparar(self, clinic, trilha):
        from apps.automation.sequences import resolver_disparo

        # Todas as trilhas semeadas, e não só a operacional: quem rodou o lote
        # ou o recorte quer ver ESSES disparos saindo, que são o que ele pediu.
        trilhas = Sequence.objects.filter(
            clinic=clinic, name__in=[SEQUENCIA, JORNADA, RESGATE]
        )
        ativas = list(
            SequenceEnrollment.objects.filter(
                sequence__in=trilhas, status=SequenceEnrollmentStatus.ACTIVE
            ).select_related("contact", "sequence")
        )
        if not ativas:
            self.stdout.write(self.style.WARNING("Nenhuma inscrição ativa para disparar."))
            return

        SequenceEnrollment.objects.filter(pk__in=[e.pk for e in ativas]).update(
            next_dispatch_at=timezone.now() - timedelta(minutes=1)
        )
        self.stdout.write("")
        self.stdout.write(f"Vencendo {len(ativas)} disparo(s) e resolvendo agora:")
        for enrollment in ativas:
            resultado = resolver_disparo(enrollment.pk)
            self.stdout.write(
                f"  [{enrollment.sequence.name}] {enrollment.contact.wa_id} → {resultado}"
            )

    # ---- limpeza ----

    def _limpar(self, clinic):
        # ⚠️ `all_objects` nos DOIS lados. Apagar a sequência é SOFT, então uma
        # trilha semeada numa rodada anterior já não aparece no gerenciador
        # padrão, e as inscrições dela ficariam órfãs para sempre - foi
        # exatamente assim que o defeito da sequência apagada apareceu.
        trilhas = Sequence.all_objects.filter(clinic=clinic, name__in=[SEQUENCIA, JORNADA, RESGATE])
        inscricoes = SequenceEnrollment.all_objects.filter(sequence__in=trilhas).count()
        SequenceEnrollment.all_objects.filter(sequence__in=trilhas).hard_delete()
        from apps.scheduling.models import Appointment, Practitioner

        Appointment.objects.filter(clinic=clinic, practitioner__name="Dra. Teste").delete()
        Practitioner.objects.filter(clinic=clinic, name="Dra. Teste").delete()
        for trilha in trilhas:
            trilha.hard_delete()

        # ⚠️ Contato e paciente saem por SOFT delete, e não é escolha: o
        # `Conversation.contact` é RESTRICT, então apagar de verdade exigiria
        # cascatear conversa, mensagem e execução à mão - o modelo protege a
        # thread justamente para isso não acontecer por acidente. A conta é que
        # rodadas repetidas deixam linhas mortas no banco de desenvolvimento,
        # invisíveis para o app e sem atrapalhar a unicidade (as constraints do
        # projeto valem só entre vivos).
        contatos = Contact.objects.filter(clinic=clinic, wa_id__startswith=PREFIXO)
        pacientes = Patient.objects.filter(clinic=clinic).filter(
            Q(name__in=[n for n, _ in PACIENTES])
            | Q(name__startswith="Resgate ")
            | Q(name__startswith="Recorte ")
        )
        fluxos = Flow.objects.filter(clinic=clinic, name__startswith="Sequência: ")

        apagados = (contatos.count(), pacientes.count(), fluxos.count())
        contatos.delete()
        pacientes.delete()
        fluxos.delete()
        WhatsAppTemplate.objects.filter(clinic=clinic, name=TEMPLATE).delete()

        self.stdout.write(
            self.style.SUCCESS(
                f"Removidos: {trilhas.count()} sequência(s) ({inscricoes} inscrição(ões)), "
                f"{apagados[0]} contato(s) 5500, {apagados[1]} paciente(s), {apagados[2]} fluxo(s)."
            )
        )
