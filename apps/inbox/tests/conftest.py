from datetime import timedelta

import pytest
from django.utils import timezone

from apps.inbox.choices import MessageKind, SenderKind, WhatsAppProviderKind
from apps.inbox.models import Channel, Conversation, Message
from apps.patients.models import Contact, Patient


def _scaffold(clinic, wa_id):
    channel = Channel.objects.create(
        clinic=clinic,
        provider=WhatsAppProviderKind.FAKE,
        display_number="5585999990000",
    )
    contact = Contact.objects.create(clinic=clinic, wa_id=wa_id, display_name="Fulano")
    patient = Patient.objects.create(clinic=clinic, name="Paciente Teste")
    conversation = Conversation.objects.create(
        clinic=clinic, channel=channel, contact=contact, patient=patient
    )
    return {
        "channel": channel,
        "contact": contact,
        "patient": patient,
        "conversation": conversation,
    }


@pytest.fixture
def inbox_a(clinic_a):
    return _scaffold(clinic_a, wa_id="5585900000001")


@pytest.fixture
def inbox_b(clinic_b):
    return _scaffold(clinic_b, wa_id="5585900000002")


def make_message(conversation, *, sender_kind=SenderKind.CONTACT, body="oi", minutes_ago=0, mid=""):
    return Message.objects.create(
        clinic=conversation.clinic,
        conversation=conversation,
        provider_message_id=mid,
        sender_kind=sender_kind,
        kind=MessageKind.TEXT,
        body=body,
        wa_timestamp=timezone.now() - timedelta(minutes=minutes_ago),
    )
