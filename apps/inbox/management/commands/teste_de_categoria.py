"""
Descobre se o que não chega no aparelho é a CATEGORIA da mensagem.

    python manage.py teste_de_categoria --clinic 3 --numero 5589XXXXXXXX --confirmo
    python manage.py teste_de_categoria --clinic 3 --numero 5589XXXXXXXX --situacao

Nasceu de um caso real em 18/08/2026: cinco mensagens de sequência saíram, a
Meta confirmou ENTREGA das cinco com identificador, e o dono do aparelho, com
o celular na mão, não viu nenhuma. As cinco eram template de **marketing**. No
mesmo dia, um texto livre mandado pelo Inbox chegou normalmente.

O teste manda duas mensagens seguidas para o MESMO número, mudando só a
categoria, e pergunta qual apareceu:

  · `hello_world` .................. UTILITY
  · resgate de inativos ............ MARKETING

Se a utilitária aparece e a de marketing não, a resposta é a categoria, e isso
muda o produto: campanha de resgate para quem não interage com a clínica vira
aposta, não certeza.

⚠️ **Manda mensagem DE VERDADE**, por isso exige `--confirmo`. Duas, não mais.

⚠️ O que este teste NÃO separa: se o filtro é da categoria ou de repetição do
mesmo conteúdo. Os três templates de marketing aprovados nesta conta já foram
entregues antes, então não existe um "marketing inédito" para usar. Separar os
dois exigiria aprovar um template novo na Meta.
"""

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone

from apps.inbox.choices import MessageKind, SenderKind
from apps.inbox.models import Channel, Conversation, Message, WhatsAppTemplate
from apps.patients.models import Contact
from apps.tenants.models import Clinic

# (rótulo, nome do template, idioma). Os dois SEM variável, para o teste não
# depender de preencher nada.
UTILITARIA = ("utilitária", "hello_world", "en_US")
DE_MARKETING = ("de marketing", "resgate_de_inativos_primeiro_convite", "pt_BR")


class Command(BaseCommand):
    help = "Manda uma mensagem utilitária e uma de marketing, para comparar."

    def add_arguments(self, parser):
        parser.add_argument("--clinic", type=int, required=True)
        parser.add_argument("--numero", required=True)
        parser.add_argument("--confirmo", action="store_true")
        parser.add_argument("--situacao", action="store_true")

    def handle(self, *args, **opts):
        try:
            clinic = Clinic.objects.get(pk=opts["clinic"])
        except Clinic.DoesNotExist:
            raise CommandError(f"Clínica {opts['clinic']} não existe.")

        numero = opts["numero"]
        contato = Contact.objects.filter(
            clinic=clinic, wa_id__endswith=numero[-8:]
        ).first()
        if contato is None:
            raise CommandError(f"{numero} não é contato desta clínica.")

        if opts["situacao"]:
            return self._situacao(clinic, contato)

        if not opts["confirmo"]:
            raise CommandError(
                f"Isto manda DUAS mensagens de verdade para {contato.wa_id}. "
                "Repita com --confirmo."
            )
        self._mandar(clinic, contato)

    def _canal(self, clinic):
        canal = Channel.objects.filter(clinic=clinic, is_test=False).first()
        if canal is None:
            raise CommandError("A clínica não tem canal de WhatsApp.")
        return canal

    def _mandar(self, clinic, contato):
        from apps.inbox.services import send_message

        canal = self._canal(clinic)
        conversa, _ = Conversation.objects.get_or_create(
            clinic=clinic, channel=canal, contact=contato
        )

        for rotulo, nome, idioma in (UTILITARIA, DE_MARKETING):
            modelo = WhatsAppTemplate.objects.filter(
                clinic=clinic, name=nome, status="APPROVED"
            ).first()
            if modelo is None:
                raise CommandError(f"O modelo {nome} não está aprovado nesta conta.")

            corpo = ""
            for c in modelo.components if isinstance(modelo.components, list) else []:
                if c.get("type") == "BODY":
                    corpo = c.get("text") or ""

            message = Message.objects.create(
                clinic=clinic,
                conversation=conversa,
                kind=MessageKind.TEMPLATE,
                sender_kind=SenderKind.BOT,
                body=corpo,
                template_name=nome,
                wa_timestamp=timezone.now(),
            )
            # Envio SÍNCRONO e um de cada vez, de propósito: pela fila as duas
            # sairiam em paralelo e a ordem de chegada não diria nada.
            send_message(message)
            message.refresh_from_db()

            marca = "SAIU" if message.provider_message_id else "NÃO SAIU"
            self.stdout.write(
                f"  {rotulo:14} {nome[:38]:38} {marca} · status={message.status!r}"
            )
            if message.status_error:
                self.stdout.write(self.style.WARNING(f"      motivo: {message.status_error}"))

        self.stdout.write("")
        self.stdout.write(
            self.style.WARNING(
                "Agora olhe o aparelho e veja QUAIS apareceram. Depois rode:\n"
                f"  manage.py teste_de_categoria --clinic {clinic.pk} "
                f"--numero {contato.wa_id} --situacao"
            )
        )

    def _situacao(self, clinic, contato):
        nomes = [UTILITARIA[1], DE_MARKETING[1]]
        qs = Message.objects.filter(
            clinic=clinic,
            conversation__contact=contato,
            template_name__in=nomes,
        ).order_by("-id")[:6]

        self.stdout.write("As últimas de cada categoria, do que a Meta respondeu:")
        for m in qs:
            categoria = "UTILITY" if m.template_name == UTILITARIA[1] else "MARKETING"
            self.stdout.write(
                f"  {m.created_at:%d/%m %H:%M} {categoria:10} {m.template_name[:36]:36} "
                f"status={m.status!r}"
            )
        self.stdout.write("")
        self.stdout.write(
            "⚠️ 'delivered' aqui quer dizer que a Meta entregou ao APARELHO. Se "
            "uma delas está entregue e você não a viu na tela, o filtro é do "
            "lado do WhatsApp, e não do nosso envio."
        )
