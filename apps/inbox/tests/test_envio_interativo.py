"""
Envio de botões e lista (F2.6).

O caminho de saída do Inbox tinha texto, template e anexo; o motor de fluxos
precisa dos dois interativos. A LEITURA da resposta já existia
(`content_data["interactive_id"]`, em `events.py`) - o que faltava era mandar.
"""

import pytest
from django.utils import timezone

from apps.inbox.choices import MessageKind, MessageStatus, SenderKind
from apps.inbox.models import Message
from apps.inbox.services import send_message

pytestmark = pytest.mark.django_db


def mensagem_do_bot(conversation, *, body="", content_data=None, kind=MessageKind.INTERACTIVE):
    return Message.objects.create(
        clinic=conversation.clinic,
        conversation=conversation,
        kind=kind,
        sender_kind=SenderKind.BOT,
        body=body,
        content_data=content_data or {},
        wa_timestamp=timezone.now(),
    )


class TestEnvioDeBotoes:
    def test_botoes_saem_com_id_e_titulo(self, inbox_a):
        message = mensagem_do_bot(
            inbox_a["conversation"],
            body="O que deseja?",
            content_data={
                "buttons": [
                    {"id": "agendar", "title": "Marcar consulta"},
                    {"id": "humano", "title": "Falar com atendente"},
                ]
            },
        )

        send_message(message)

        message.refresh_from_db()
        assert message.provider_message_id
        assert message.status == MessageStatus.SENT

    def test_lista_sai_com_secoes(self, inbox_a):
        message = mensagem_do_bot(
            inbox_a["conversation"],
            body="Escolha a especialidade",
            content_data={
                "list": {
                    "button_label": "Ver especialidades",
                    "sections": [
                        {
                            "title": "Especialidades",
                            "rows": [{"id": "cardio", "title": "Cardiologia"}],
                        }
                    ],
                }
            },
        )

        send_message(message)

        message.refresh_from_db()
        assert message.provider_message_id

    def test_mensagem_comum_continua_indo_como_texto(self, inbox_a):
        """A rota nova não pode ter mudado o caminho que já funcionava."""
        message = mensagem_do_bot(inbox_a["conversation"], body="Bom dia", kind=MessageKind.TEXT)

        send_message(message)

        message.refresh_from_db()
        assert message.provider_message_id
        assert message.status == MessageStatus.SENT

    def test_nota_interna_com_botoes_continua_sem_sair(self, inbox_a):
        """
        A guarda da nota interna (RF-ATD-3) vem ANTES do roteamento por tipo -
        acrescentar rota nova não pode ter aberto um caminho de fuga.
        """
        message = mensagem_do_bot(
            inbox_a["conversation"],
            body="Combinado com a Ana",
            content_data={"buttons": [{"id": "x", "title": "X"}]},
        )
        message.is_internal = True
        message.save(update_fields=["is_internal"])

        send_message(message)

        message.refresh_from_db()
        assert message.provider_message_id == ""
