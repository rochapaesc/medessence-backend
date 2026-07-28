"""
Cenários de demonstração do Inbox (F2.5, §4.3.1) - para ver a tela em todos os
estados do ciclo de vida e da posse sem depender do que o dia trouxe.

SEGURANÇA. Este comando roda contra o banco real, então ele foi desenhado para
não ter como encostar em dado de verdade:

- Todo contato criado usa o prefixo `5500` (DDD 00 NÃO EXISTE no Brasil). É um
  número impossível: mesmo que alguma fase futura tente enviar, não há para
  quem entregar - e nenhuma pessoa real vai receber mensagem de teste.
- `--limpar` apaga EXATAMENTE os contatos desse prefixo e o que pende deles.
  Não há filtro por nome, data ou "criado recentemente", que pegariam junto o
  que não é meu.
- Mensagem criada pelo ORM não enfileira envio (o único signal de Message é o
  de denormalização) - nada sai para a Meta.

    python manage.py seed_inbox_demo --clinic 3
    python manage.py seed_inbox_demo --clinic 3 --limpar
"""

from datetime import timedelta
from zoneinfo import ZoneInfo

from django.conf import settings
from django.core.management.base import BaseCommand, CommandError
from django.db import transaction
from django.utils import timezone

from apps.inbox.choices import (
    ActivityType,
    AttendedBy,
    ConversationStatus,
    MessageDirection,
    MessageKind,
    MessageStatus,
    SenderKind,
)
from apps.inbox.models import Channel, Conversation, Message
from apps.patients.models import Contact

# DDD 00 não existe: número impossível de existir e de receber mensagem.
PREFIXO_DEMO = "5500"


class Command(BaseCommand):
    help = "Cria (ou remove) conversas de demonstração do Inbox."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True, help="Id da clínica.")
        parser.add_argument(
            "--limpar",
            action="store_true",
            help="Remove os cenários de demonstração em vez de criá-los.",
        )
        parser.add_argument(
            "--dono",
            default="atendente.medessence@medessence.dev",
            help="E-mail de quem 'sou eu' nos cenários (conversa livre para escrever).",
        )
        parser.add_argument(
            "--outro",
            default="gestor.medessence@medessence.dev",
            help="E-mail do colega que segura a conversa travada.",
        )

    def handle(self, *args, **options):
        clinic_id = options["clinic"]
        if options["limpar"]:
            return self._limpar(clinic_id)
        return self._criar(clinic_id, options["dono"], options["outro"])

    # ------------------------------------------------------------------ #

    def _limpar(self, clinic_id):
        contatos = Contact.objects.filter(clinic_id=clinic_id, wa_id__startswith=PREFIXO_DEMO)
        conversas = Conversation.objects.filter(contact__in=contatos)
        mensagens = Message.objects.filter(conversation__in=conversas)

        # Ordem importa: Conversation.contact é RESTRICT.
        n_msg = mensagens.count()
        n_conv = conversas.count()
        n_cont = contatos.count()
        mensagens.hard_delete()
        conversas.hard_delete()
        contatos.hard_delete()
        self.stdout.write(
            self.style.SUCCESS(
                f"Removidos: {n_cont} contatos, {n_conv} conversas, {n_msg} mensagens."
            )
        )

    @transaction.atomic
    def _criar(self, clinic_id, email_dono, email_outro):
        from apps.accounts.models import User

        canal = Channel.objects.filter(clinic_id=clinic_id).first()
        if canal is None:
            raise CommandError(f"A clínica {clinic_id} não tem canal de WhatsApp.")

        eu = User.objects.filter(email=email_dono).first()
        colega = User.objects.filter(email=email_outro).first()
        if eu is None or colega is None:
            raise CommandError(
                f"Preciso dos dois usuários: {email_dono} e {email_outro}. "
                "Passe --dono/--outro se os e-mails forem outros."
            )

        agora = timezone.now()
        # "Volta às 9h" é 9h no relógio da CLÍNICA. Montar em UTC faria o
        # adiamento aparecer às 6h na tela — o mesmo erro de fuso que a
        # contagem da agenda já pagou uma vez.
        fuso = ZoneInfo(canal.clinic.timezone or settings.TIME_ZONE)

        criados = 0
        for indice, cenario in enumerate(self._cenarios(agora, eu, colega), start=1):
            self._montar(clinic_id, canal, indice, cenario, agora, fuso)
            criados += 1

        self.stdout.write(
            self.style.SUCCESS(
                f"{criados} conversas de demonstração na clínica {clinic_id}. "
                f"Para remover: --limpar"
            )
        )

    # ------------------------------------------------------------------ #

    def _cenarios(self, agora, eu, colega):
        """
        Um cenário por estado visível da tela. `mensagens` é uma lista de
        tuplas (minutos_atras, tipo, dados) - o tempo relativo mantém a thread
        coerente sempre que o seed roda.
        """
        return [
            # 1. A fila normal: ninguém pegou ainda, paciente esperando.
            {
                "nome": "Larissa Melo",
                "status": ConversationStatus.WAITING,
                "attended_by": AttendedBy.NONE,
                "inbound_ha_min": 12,
                "unread": 2,
                "mensagens": [
                    (34, "in", "Oi! Queria remarcar minha consulta de quinta"),
                    (13, "in", "Consigo na sexta de manhã?"),
                    (12, "in", "Ou segunda, se for melhor"),
                ],
            },
            # 2. IA conduzindo: faixa teal, composer travado.
            {
                "nome": "Rafael Alves",
                "status": ConversationStatus.OPEN,
                "attended_by": AttendedBy.BOT,
                "attended_ha_min": 26,
                "inbound_ha_min": 8,
                "unread": 1,
                "mensagens": [
                    (30, "in", "Boa tarde, qual o valor da avaliação?"),
                    (26, "ev", (ActivityType.BOT_STARTED, None, {})),
                    (
                        25,
                        "bot",
                        "Boa tarde! A avaliação inicial custa R$ 250. "
                        "Posso verificar os horários disponíveis para você?",
                    ),
                    (8, "in", "Pode sim, prefiro de manhã"),
                ],
            },
            # 3. Colega conduzindo: faixa âmbar, "Assumir" é o único caminho.
            {
                "nome": "Joana Ribeiro",
                "status": ConversationStatus.OPEN,
                "attended_by": AttendedBy.AGENT,
                "assigned_to": colega,
                "attended_ha_min": 41,
                "inbound_ha_min": 15,
                "mensagens": [
                    (48, "in", "Bom dia, gostaria de saber se atendem pelo meu convênio"),
                    (41, "ev", (ActivityType.ASSIGNED, colega, {"from": AttendedBy.NONE})),
                    (40, "out", "Bom dia, Joana! Qual é o seu convênio?"),
                    (15, "in", "Unimed"),
                ],
            },
            # 4. Minha: composer livre, com nota da equipe na thread.
            {
                "nome": "Carlos Menezes",
                "status": ConversationStatus.OPEN,
                "attended_by": AttendedBy.AGENT,
                "assigned_to": eu,
                "attended_ha_min": 55,
                "inbound_ha_min": 20,
                "mensagens": [
                    (62, "in", "Preciso remarcar a consulta do meu filho"),
                    (55, "ev", (ActivityType.ASSIGNED, eu, {"from": AttendedBy.NONE})),
                    (54, "out", "Claro! Ele tem consulta marcada para amanhã às 14h. "
                                "Qual dia fica melhor?"),
                    (
                        45,
                        "nota",
                        "Convênio dele mudou para Unimed. Confere na ficha antes de "
                        "passar valor — a tabela do particular não vale aqui.",
                        eu,
                    ),
                    (20, "in", "Vou confirmar com a mãe dele e te falo"),
                ],
            },
            # 5. Adiada: só aparece com o filtro Adiadas, com o "volta quando".
            {
                "nome": "Beatriz Nunes",
                "status": ConversationStatus.SNOOZED,
                "attended_by": AttendedBy.AGENT,
                "assigned_to": eu,
                "attended_ha_min": 200,
                "inbound_ha_min": 180,
                "snoozed_dias": 2,
                "mensagens": [
                    (190, "in", "Oi, vocês parcelam o tratamento?"),
                    (185, "out", "Oi, Beatriz! Parcelamos em até 6x sem juros."),
                    (180, "in", "Legal, vou conversar em casa e te retorno"),
                    (176, "nota", "Vai confirmar com o marido. Retomar na segunda.", eu),
                    (175, "ev", (ActivityType.SNOOZED, eu, "SNOOZE_UNTIL")),
                ],
            },
            # 6. Resolvida: some da fila viva, cabeçalho vira "Reabrir".
            {
                "nome": "Marcos Tavares",
                "status": ConversationStatus.RESOLVED,
                "attended_by": AttendedBy.AGENT,
                "assigned_to": eu,
                "attended_ha_min": 300,
                "inbound_ha_min": 280,
                "resolved_ha_min": 270,
                "mensagens": [
                    (290, "in", "Consegui remarcar pelo telefone, obrigado!"),
                    (285, "out", "Que ótimo, Marcos! Ficou para sexta às 10h. Até lá!"),
                    (280, "in", "Perfeito, obrigado!"),
                    (271, "nota", "Remarcou para sexta 10h. Convênio novo conferido.", eu),
                    (270, "ev", (ActivityType.RESOLVED, eu, {})),
                ],
            },
            # 7. Janela de 24h FECHADA: composer só oferece template aprovado.
            {
                "nome": "Patrícia Gomes",
                "status": ConversationStatus.WAITING,
                "attended_by": AttendedBy.NONE,
                "inbound_ha_min": 36 * 60,
                "unread": 1,
                "mensagens": [
                    (37 * 60, "in", "Boa tarde, vocês atendem no sábado?"),
                    (36 * 60, "in", "Alguém pode me responder?"),
                ],
            },
            # 8. IA entregou para humano: o resumo é o que diz onde parou.
            {
                "nome": "Sofia Andrade",
                "status": ConversationStatus.WAITING,
                "attended_by": AttendedBy.NONE,
                "inbound_ha_min": 5,
                "unread": 1,
                "mensagens": [
                    (22, "in", "Oi, tenho uma dúvida sobre o preparo do exame"),
                    (21, "ev", (ActivityType.BOT_STARTED, None, {})),
                    (
                        20,
                        "bot",
                        "Oi, Sofia! O preparo pede jejum de 8 horas. "
                        "Posso ajudar em mais alguma coisa?",
                    ),
                    (7, "in", "É que eu tomo remédio de manhã, posso tomar mesmo assim?"),
                    (
                        6,
                        "ev",
                        (
                            ActivityType.BOT_HANDOFF,
                            None,
                            {"summary": "dúvida clínica sobre medicação em jejum"},
                        ),
                    ),
                    (5, "in", "Consegue me confirmar?"),
                ],
            },
        ]

    # ------------------------------------------------------------------ #

    def _montar(self, clinic_id, canal, indice, cenario, agora, fuso):
        contato, _ = Contact.objects.get_or_create(
            clinic_id=clinic_id,
            wa_id=f"{PREFIXO_DEMO}{indice:09d}",
            defaults={"display_name": cenario["nome"]},
        )
        conversa, _ = Conversation.objects.get_or_create(
            clinic_id=clinic_id, channel=canal, contact=contato
        )
        Message.objects.filter(conversation=conversa).hard_delete()

        adiado_para = (agora + timedelta(days=cenario.get("snoozed_dias", 0))).astimezone(fuso)
        adiado_para = adiado_para.replace(hour=9, minute=0, second=0, microsecond=0)

        for item in cenario["mensagens"]:
            minutos, tipo = item[0], item[1]
            quando = agora - timedelta(minutes=minutos)
            self._mensagem(clinic_id, conversa, tipo, item, quando, adiado_para)

        # Estado final por UPDATE direto: o signal de Message já mexeu em prévia
        # e contadores (e a reabertura no inbound desfaria adiada/resolvida).
        # Aqui é o retrato que se quer ver na tela, não o efeito colateral.
        Conversation.objects.filter(pk=conversa.pk).update(
            status=cenario["status"],
            attended_by=cenario["attended_by"],
            assigned_to=cenario.get("assigned_to"),
            attended_since=(
                agora - timedelta(minutes=cenario["attended_ha_min"])
                if cenario.get("attended_ha_min")
                else None
            ),
            waiting_since=(
                agora - timedelta(minutes=cenario["inbound_ha_min"])
                if cenario["status"] == ConversationStatus.WAITING
                else None
            ),
            snoozed_until=(
                adiado_para if cenario["status"] == ConversationStatus.SNOOZED else None
            ),
            resolved_at=(
                agora - timedelta(minutes=cenario["resolved_ha_min"])
                if cenario.get("resolved_ha_min")
                else None
            ),
            unread_count=cenario.get("unread", 0),
            last_inbound_at=agora - timedelta(minutes=cenario["inbound_ha_min"]),
        )

    def _mensagem(self, clinic_id, conversa, tipo, item, quando, adiado_para):
        comum = {"clinic_id": clinic_id, "conversation": conversa, "wa_timestamp": quando}

        if tipo == "ev":
            activity, autor, dados = item[2]
            if dados == "SNOOZE_UNTIL":
                dados = {"until": adiado_para.isoformat()}
            return Message.objects.create(
                **comum,
                kind=MessageKind.ACTIVITY,
                sender_kind=SenderKind.SYSTEM,
                sent_by=autor,
                activity_type=activity,
                activity_data=dados,
            )

        if tipo == "nota":
            return Message.objects.create(
                **comum,
                kind=MessageKind.TEXT,
                direction=MessageDirection.OUT,
                sender_kind=SenderKind.AGENT,
                sent_by=item[3],
                body=item[2],
                is_internal=True,
            )

        if tipo == "in":
            return Message.objects.create(
                **comum,
                kind=MessageKind.TEXT,
                direction=MessageDirection.IN,
                sender_kind=SenderKind.CONTACT,
                body=item[2],
                # wamid de demonstração: o prefixo denuncia a origem se algum dia
                # aparecer num log.
                provider_message_id=f"wamid.DEMO-{conversa.pk}-{int(quando.timestamp())}",
            )

        return Message.objects.create(
            **comum,
            kind=MessageKind.TEXT,
            direction=MessageDirection.OUT,
            sender_kind=SenderKind.BOT if tipo == "bot" else SenderKind.AGENT,
            body=item[2],
            status=MessageStatus.READ,
            provider_message_id=f"wamid.DEMO-{conversa.pk}-{int(quando.timestamp())}",
        )
