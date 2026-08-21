"""
Conserta os balões VAZIOS que o parser antigo deixou na conversa.

Até 21/08/2026, `revoke`, `edit` e `system` não estavam no mapa de tipos:
caíam em "não suportado" e viravam MENSAGENS - balões sem conteúdo nenhum na
tela da recepção, com o `original_message_id` jogado fora.

O payload cru de cada uma ficou guardado em `raw_payload`, então dá para
aplicar o efeito que deveria ter acontecido e remover o balão:

    manage.py reprocessar_acoes                # ensaio: só conta e mostra
    manage.py reprocessar_acoes --apply        # aplica
    manage.py reprocessar_acoes --apply --clinic 3

⚠️ Ensaio por padrão. Ele mexe em conversa de clínica em produção, e o
--apply é o ato consciente.
"""

from django.core.management.base import BaseCommand
from django.db import transaction

from apps.inbox.choices import MessageKind
from apps.inbox.models import Message

# Só estes: são os que o parser antigo transformava em balão vazio.
ACOES = {"revoke", "edit", "system"}


class Command(BaseCommand):
    help = "Aplica os revoke/edit/system que viraram balão vazio e remove os balões."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true", help="Grava as mudanças.")
        parser.add_argument("--clinic", type=int, help="Limita a uma clínica.")

    def handle(self, *args, **options):
        from apps.inbox.models import Channel
        from apps.inbox.services import (
            _aplicar_edit,
            _aplicar_revoke,
            _aplicar_troca_de_numero,
        )
        from apps.integrations.whatsapp.base import WhatsAppEventKind
        from apps.integrations.whatsapp.events import _parse_message

        aplicar = options["apply"]
        candidatas = Message.objects.filter(kind=MessageKind.UNSUPPORTED)
        if options.get("clinic"):
            candidatas = candidatas.filter(clinic_id=options["clinic"])

        contas = {"revoke": 0, "edit": 0, "system": 0, "sem_alvo": 0, "ignoradas": 0}
        for mensagem in candidatas.select_related("conversation", "clinic"):
            bruto = mensagem.raw_payload or {}
            tipo = bruto.get("type")
            if tipo not in ACOES:
                contas["ignoradas"] += 1
                continue

            canal = Channel.objects.filter(clinic=mensagem.clinic).first()
            if canal is None:
                contas["sem_alvo"] += 1
                continue

            evento = _parse_message(
                bruto,
                kind={
                    "revoke": WhatsAppEventKind.REVOKE,
                    "edit": WhatsAppEventKind.EDIT,
                    "system": WhatsAppEventKind.NUMBER_CHANGE,
                }[tipo],
                wa_id=bruto.get("from", ""),
                names={},
            )
            aplicador = {
                "revoke": _aplicar_revoke,
                "edit": _aplicar_edit,
                "system": _aplicar_troca_de_numero,
            }[tipo]

            if not aplicar:
                contas[tipo] += 1
                self.stdout.write(
                    f"  #{mensagem.pk} (conversa {mensagem.conversation_id}): {tipo}"
                )
                continue

            with transaction.atomic():
                if aplicador(canal, evento):
                    contas[tipo] += 1
                else:
                    # O alvo pode não existir (apagou algo anterior à conexão).
                    # O balão vazio sai mesmo assim: ele não informa nada.
                    contas["sem_alvo"] += 1
                mensagem.delete()

        self.stdout.write("")
        for chave, valor in contas.items():
            self.stdout.write(f"  {chave:10} {valor}")
        if not aplicar:
            self.stdout.write(
                self.style.WARNING("\nEnsaio. Rode com --apply para valer.")
            )
        else:
            self.stdout.write(self.style.SUCCESS("\nBalões vazios reprocessados."))
