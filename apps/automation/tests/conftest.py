import pytest

from apps.automation.choices import FlowStatus, FlowTrigger
from apps.automation.models import Flow, FlowVersion
from apps.inbox.choices import WhatsAppProviderKind
from apps.inbox.models import Channel, Conversation
from apps.patients.models import Contact


def make_flow(clinic, *, name="Agendamento", status=FlowStatus.DRAFT, graph=None, **extra):
    """Fluxo com uma versão publicada, que é o estado mínimo para executar."""
    flow = Flow.objects.create(
        clinic=clinic,
        name=name,
        status=status,
        trigger=extra.pop("trigger", FlowTrigger.FIRST_INBOUND),
        **extra,
    )
    version = FlowVersion.objects.create(
        flow=flow,
        number=1,
        graph=graph or {"nodes": [], "edges": [], "entry_node": ""},
    )
    flow.current_version = version
    flow.save(update_fields=["current_version"])
    return flow


def make_contact(clinic, wa_id="5585900000001"):
    return Contact.objects.create(clinic=clinic, wa_id=wa_id, display_name="Fulano")


def make_channel(clinic):
    return Channel.objects.create(
        clinic=clinic,
        provider=WhatsAppProviderKind.FAKE,
        display_number="5585999990000",
    )


def make_conversation(clinic, contact, channel=None):
    return Conversation.objects.create(
        clinic=clinic, channel=channel or make_channel(clinic), contact=contact
    )


def make_inbox(clinic, wa_id="5585900000001"):
    """Canal + contato + conversa, para exercitar a ingestão de ponta a ponta."""
    channel = make_channel(clinic)
    contact = make_contact(clinic, wa_id)
    return {
        "channel": channel,
        "contact": contact,
        "conversation": make_conversation(clinic, contact, channel),
    }


@pytest.fixture
def flow_a(clinic_a):
    return make_flow(clinic_a)


@pytest.fixture
def contact_a(clinic_a):
    return make_contact(clinic_a)
