"""Regras de modelo do inbox: direção derivada, idempotência, unicidade e
denormalização por signal."""

from datetime import timedelta

import pytest
from django.db import IntegrityError
from django.utils import timezone

from apps.inbox.choices import MessageDirection, SenderKind
from apps.inbox.models import Channel
from apps.inbox.tests.conftest import make_message


@pytest.mark.parametrize(
    ("sender_kind", "expected"),
    [
        (SenderKind.CONTACT, MessageDirection.IN),
        (SenderKind.AGENT, MessageDirection.OUT),
        (SenderKind.BOT, MessageDirection.OUT),
    ],
)
def test_direction_derived_from_sender_kind(inbox_a, sender_kind, expected):
    message = make_message(inbox_a["conversation"], sender_kind=sender_kind)
    assert message.direction == expected


def test_wamid_unique_por_clinica(inbox_a):
    make_message(inbox_a["conversation"], mid="wamid-1")
    with pytest.raises(IntegrityError):
        make_message(inbox_a["conversation"], mid="wamid-1")


def test_wamid_vazio_nao_colide(inbox_a):
    # provider_message_id em branco (mensagem do composer) não entra na unicidade.
    make_message(inbox_a["conversation"], mid="")
    make_message(inbox_a["conversation"], mid="")  # não levanta


def test_one_channel_per_clinic(clinic_a, inbox_a):
    with pytest.raises(IntegrityError):
        Channel.objects.create(clinic=clinic_a, display_number="outro")


def test_inbound_atualiza_conversa(inbox_a):
    conversation = inbox_a["conversation"]
    make_message(conversation, sender_kind=SenderKind.CONTACT, body="preciso remarcar")
    conversation.refresh_from_db()
    assert conversation.unread_count == 1
    assert conversation.last_inbound_at is not None
    assert conversation.last_message_preview == "preciso remarcar"
    assert conversation.window_open is True


def test_outbound_nao_conta_como_nao_lida(inbox_a):
    conversation = inbox_a["conversation"]
    make_message(conversation, sender_kind=SenderKind.AGENT, body="olá")
    conversation.refresh_from_db()
    assert conversation.unread_count == 0
    assert conversation.last_inbound_at is None
    assert conversation.window_open is False


def test_window_fecha_apos_24h(inbox_a):
    conversation = inbox_a["conversation"]
    conversation.last_inbound_at = timezone.now() - timedelta(hours=25)
    conversation.save(update_fields=["last_inbound_at"])
    assert conversation.window_open is False
