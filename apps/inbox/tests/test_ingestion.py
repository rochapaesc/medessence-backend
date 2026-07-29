"""Ingestão do webhook (§7): parser Meta, idempotência, echo, status, mídia."""

from apps.inbox.choices import MessageDirection, MessageKind, MessageStatus, SenderKind
from apps.inbox.models import Conversation, MediaAsset, Message
from apps.inbox.services import ingest_events
from apps.integrations.whatsapp.events import parse_meta_webhook
from apps.integrations.whatsapp.fake.adapter import build_inbound_payload


def _status_payload(wamid, status, recipient="5585900000009"):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {
                                    "id": wamid,
                                    "status": status,
                                    "recipient_id": recipient,
                                    "timestamp": "1710000100",
                                }
                            ]
                        }
                    }
                ]
            }
        ]
    }


def _media_payload(wa_id, media_id):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": wa_id, "profile": {"name": "Contato"}}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": f"wamid.{media_id}",
                                    "timestamp": "1710000200",
                                    "type": "image",
                                    "image": {
                                        "id": media_id,
                                        "mime_type": "image/jpeg",
                                        "caption": "foto",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


def test_parse_inbound_text(db):
    payload = build_inbound_payload(wa_id="5585911112222", body="oi", name="Ana")
    events = parse_meta_webhook(payload)
    assert len(events) == 1
    assert events[0].kind == "inbound"
    assert events[0].wa_id == "5585911112222"
    assert events[0].body == "oi"
    assert events[0].contact_name == "Ana"


def test_ingest_inbound_cria_conversa_e_mensagem(clinic_a, inbox_a):
    channel = inbox_a["channel"]
    payload = build_inbound_payload(wa_id="5585933334444", body="quero remarcar")
    stats = ingest_events(channel, parse_meta_webhook(payload))
    assert stats["inbound"] == 1

    message = Message.objects.get(clinic=clinic_a, body="quero remarcar")
    assert message.direction == MessageDirection.IN
    assert message.sender_kind == SenderKind.CONTACT
    conversation = message.conversation
    assert conversation.contact.wa_id == "5585933334444"
    assert conversation.unread_count == 1
    assert conversation.window_open is True


def test_ingest_idempotente_no_replay(clinic_a, inbox_a):
    channel = inbox_a["channel"]
    payload = build_inbound_payload(wa_id="5585955556666", body="olá")
    events = parse_meta_webhook(payload)
    ingest_events(channel, events)
    ingest_events(channel, events)  # replay do mesmo wamid
    assert Message.objects.filter(clinic=clinic_a).count() == 1


def test_ingest_auto_vincula_paciente_principal(clinic_a, inbox_a):
    from apps.patients.models import Contact, PatientContact

    channel = inbox_a["channel"]
    contact = Contact.objects.create(clinic=clinic_a, wa_id="5585977778888")
    PatientContact.objects.create(patient=inbox_a["patient"], contact=contact, is_primary=True)

    payload = build_inbound_payload(wa_id="5585977778888", body="oi")
    ingest_events(channel, parse_meta_webhook(payload))
    conversation = Conversation.objects.get(contact=contact)
    assert conversation.patient_id == inbox_a["patient"].id


def test_echo_vira_mensagem_out(clinic_a, inbox_a):
    channel = inbox_a["channel"]
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": "5585900000123", "profile": {"name": "X"}}],
                            "message_echoes": [
                                {
                                    "to": "5585900000123",
                                    "id": "wamid.echo1",
                                    "timestamp": "1710000300",
                                    "type": "text",
                                    "text": {"body": "resposta pelo celular"},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }
    stats = ingest_events(channel, parse_meta_webhook(payload))
    assert stats["echo"] == 1
    message = Message.objects.get(clinic=clinic_a, provider_message_id="wamid.echo1")
    assert message.direction == MessageDirection.OUT
    assert message.sender_kind == SenderKind.AGENT


def test_status_atualiza_mensagem(clinic_a, inbox_a):
    from apps.inbox.tests.conftest import make_message

    message = make_message(inbox_a["conversation"], sender_kind=SenderKind.AGENT, mid="wamid.out1")
    ingest_events(inbox_a["channel"], parse_meta_webhook(_status_payload("wamid.out1", "read")))
    message.refresh_from_db()
    assert message.status == MessageStatus.READ


def test_inbound_reentregue_velho_nao_rebobina_a_janela(clinic_a, inbox_a):
    """A Meta REENTREGA webhook que falhou — às vezes um dia depois.

    Visto ao vivo em 29/07: a rajada de reentrega pós-queda trouxe mensagem da
    VÉSPERA, o relógio da janela voltou para ontem e a janela "fechou" numa
    conversa que tinha acabado de receber mensagem. O relógio só anda para
    frente; a mensagem velha entra na thread e conta como não lida.
    """
    from apps.inbox.tests.conftest import make_message

    conversation = inbox_a["conversation"]
    make_message(conversation, mid="wamid.hoje")  # inbound de agora
    conversation.refresh_from_db()
    relogio = conversation.last_inbound_at
    assert conversation.window_open is True

    # A reentrega: mensagem de ONTEM (25h) processada só agora.
    make_message(conversation, minutes_ago=25 * 60, mid="wamid.ontem")

    conversation.refresh_from_db()
    assert conversation.last_inbound_at == relogio, "o relógio não rebobina"
    assert conversation.window_open is True
    assert conversation.unread_count == 2, "a mensagem velha ainda é não lida"


def test_status_atrasado_nao_regride(clinic_a, inbox_a):
    """A Meta entrega fora de ordem: um delivered DEPOIS do read não regride.

    Achado na leitura das referências (Whatomate statusPriority) — antes desta
    guarda, o último webhook a chegar mandava, qualquer que fosse."""
    from apps.inbox.tests.conftest import make_message

    message = make_message(inbox_a["conversation"], sender_kind=SenderKind.AGENT, mid="wamid.ooo")
    channel = inbox_a["channel"]

    ingest_events(channel, parse_meta_webhook(_status_payload("wamid.ooo", "read")))
    ingest_events(channel, parse_meta_webhook(_status_payload("wamid.ooo", "delivered")))

    message.refresh_from_db()
    assert message.status == MessageStatus.READ, "delivered atrasado não desfaz o read"


def test_status_failed_guarda_o_motivo(clinic_a, inbox_a):
    """errors[] do FAILED vira texto legível em status_error — "Falhou" sem
    motivo não ajuda ninguém a agir."""
    from apps.inbox.tests.conftest import make_message

    message = make_message(inbox_a["conversation"], sender_kind=SenderKind.AGENT, mid="wamid.f1")
    payload = _status_payload("wamid.f1", "failed")
    payload["entry"][0]["changes"][0]["value"]["statuses"][0]["errors"] = [
        {
            "code": 131047,
            "title": "Re-engagement message",
            "error_data": {"details": "Mais de 24h desde a última resposta do cliente."},
        }
    ]

    ingest_events(inbox_a["channel"], parse_meta_webhook(payload))

    message.refresh_from_db()
    assert message.status == MessageStatus.FAILED
    assert "131047" in message.status_error
    assert "24h" in message.status_error


def test_status_failed_nao_desfaz_entrega_confirmada(clinic_a, inbox_a):
    """FAILED fora de ordem depois de delivered/read é ruído — não aplica."""
    from apps.inbox.tests.conftest import make_message

    message = make_message(inbox_a["conversation"], sender_kind=SenderKind.AGENT, mid="wamid.f2")
    channel = inbox_a["channel"]

    ingest_events(channel, parse_meta_webhook(_status_payload("wamid.f2", "delivered")))
    ingest_events(channel, parse_meta_webhook(_status_payload("wamid.f2", "failed")))

    message.refresh_from_db()
    assert message.status == MessageStatus.DELIVERED


def test_delivered_supera_failed_e_limpa_o_motivo(clinic_a, inbox_a):
    """Se entregou, entregou: delivered posterior supera o FAILED e o motivo
    antigo não fica assombrando a thread."""
    from apps.inbox.tests.conftest import make_message

    message = make_message(inbox_a["conversation"], sender_kind=SenderKind.AGENT, mid="wamid.f3")
    channel = inbox_a["channel"]

    payload = _status_payload("wamid.f3", "failed")
    payload["entry"][0]["changes"][0]["value"]["statuses"][0]["errors"] = [
        {"code": 1, "title": "Erro transitório"}
    ]
    ingest_events(channel, parse_meta_webhook(payload))
    ingest_events(channel, parse_meta_webhook(_status_payload("wamid.f3", "delivered")))

    message.refresh_from_db()
    assert message.status == MessageStatus.DELIVERED
    assert message.status_error == ""


def test_midia_cria_asset(clinic_a, inbox_a):
    channel = inbox_a["channel"]
    ingest_events(channel, parse_meta_webhook(_media_payload("5585900000777", "media-xyz")))
    message = Message.objects.get(clinic=clinic_a, provider_message_id="wamid.media-xyz")
    assert message.kind == MessageKind.IMAGE
    assert message.media is not None
    assert MediaAsset.objects.filter(provider_media_id="media-xyz").exists()


def test_ingest_escopado_por_canal(clinic_a, clinic_b, inbox_a, inbox_b):
    ingest_events(
        inbox_a["channel"],
        parse_meta_webhook(build_inbound_payload(wa_id="5585900001111", body="a")),
    )
    ingest_events(
        inbox_b["channel"],
        parse_meta_webhook(build_inbound_payload(wa_id="5585900002222", body="b")),
    )
    assert Message.objects.filter(clinic=clinic_a).count() == 1
    assert Message.objects.filter(clinic=clinic_b).count() == 1
    assert not Conversation.objects.filter(clinic=clinic_a, contact__wa_id="5585900002222").exists()
