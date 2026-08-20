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


# ------------------- Autocura do nono dígito (§6.2) -------------------


def test_inbound_com_grafia_alternativa_renomeia_o_contato(clinic_a, inbox_a):
    """
    Criamos o contato com o 9 (palpite do outbound-first); a Meta identifica o
    número SEM o 9 → o contato é RENOMEADO para a forma dela e a conversa é a
    MESMA — nada duplica, o vínculo com o paciente sobrevive.
    """
    from apps.patients.models import Contact

    channel = inbox_a["channel"]
    contato = Contact.objects.create(clinic=clinic_a, wa_id="5585988765432")
    conversa = Conversation.objects.create(clinic=clinic_a, channel=channel, contact=contato)

    payload = build_inbound_payload(wa_id="558588765432", body="cheguei")
    ingest_events(channel, parse_meta_webhook(payload))

    contato.refresh_from_db()
    assert contato.wa_id == "558588765432"  # a Meta é dona do wa_id
    assert Contact.objects.filter(clinic=clinic_a, wa_id__contains="88765432").count() == 1
    mensagem = Message.objects.get(clinic=clinic_a, body="cheguei")
    assert mensagem.conversation_id == conversa.pk


def test_inbound_de_numero_novo_nao_mexe_nos_existentes(clinic_a, inbox_a):
    from apps.patients.models import Contact

    channel = inbox_a["channel"]
    Contact.objects.create(clinic=clinic_a, wa_id="5585988765432")

    ingest_events(
        channel, parse_meta_webhook(build_inbound_payload(wa_id="5585911112222", body="olá"))
    )

    assert Contact.objects.filter(clinic=clinic_a, wa_id="5585988765432").exists()
    assert Contact.objects.filter(clinic=clinic_a, wa_id="5585911112222").exists()


def test_grafia_exata_existente_nao_renomeia_nada(clinic_a, inbox_a):
    """Se a grafia exata existe, a autocura nem é consultada — inclusive quando
    a alternativa TAMBÉM existe (zumbi antigo): o exato vence."""
    from apps.patients.models import Contact

    channel = inbox_a["channel"]
    exato = Contact.objects.create(clinic=clinic_a, wa_id="558588765432")
    zumbi = Contact.objects.create(clinic=clinic_a, wa_id="5585988765432")

    ingest_events(
        channel, parse_meta_webhook(build_inbound_payload(wa_id="558588765432", body="oi"))
    )

    exato.refresh_from_db()
    zumbi.refresh_from_db()
    assert exato.wa_id == "558588765432"
    assert zumbi.wa_id == "5585988765432"  # intocado
    mensagem = Message.objects.get(clinic=clinic_a, body="oi")
    assert mensagem.conversation.contact_id == exato.pk


# --------------------------------------------------------------------- #
# Reentrega de mensagem APAGADA (achado em 19/08/2026)
# --------------------------------------------------------------------- #
#
# A `uniq_message_wamid` não dispensa registro soft-deletado, então a mensagem
# apagada continua ocupando o wamid. A ingestão procurava só entre as vivas,
# tentava criar de novo e estourava IntegrityError — e como a task levanta, o
# LOTE INTEIRO do webhook se perdia. Quem chega nesse estado é o admin do
# Django (a tela do Inbox recusa apagar mensagem já entregue).


def _payload_com_duas(wa_id, wamid_repetido):
    """Um webhook com a mensagem repetida e uma nova, como a Meta reentrega."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "PID"},
                            "contacts": [{"wa_id": wa_id, "profile": {"name": "P"}}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": wamid_repetido,
                                    "timestamp": "1755600000",
                                    "type": "text",
                                    "text": {"body": "a que foi apagada"},
                                },
                                {
                                    "from": wa_id,
                                    "id": "wamid.NOVA-DO-LOTE",
                                    "timestamp": "1755600100",
                                    "type": "text",
                                    "text": {"body": "esta nunca chegou antes"},
                                },
                            ],
                        },
                    }
                ]
            }
        ]
    }


def test_reentrega_de_mensagem_apagada_nao_derruba_o_lote(clinic_a, inbox_a):
    """
    O que se perdia era a mensagem NOVA do mesmo webhook, e não a repetida:
    um paciente escrevendo naquele instante simplesmente não aparecia no Inbox.
    """
    channel = inbox_a["channel"]
    wa_id = inbox_a["contact"].wa_id
    ingest_events(
        channel, parse_meta_webhook(build_inbound_payload(wa_id=wa_id, body="original"))
    )
    apagada = Message.objects.get(clinic=clinic_a, body="original")
    apagada.delete()  # soft, como o admin fazia

    stats = ingest_events(
        channel, parse_meta_webhook(_payload_com_duas(wa_id, apagada.provider_message_id))
    )

    assert stats["inbound"] == 1, "só a nova conta; a repetida é reentrega"
    assert Message.objects.filter(clinic=clinic_a, body="esta nunca chegou antes").exists()


def test_a_mensagem_apagada_NAO_ressuscita(clinic_a, inbox_a):
    """
    A alternativa (afrouxar a constraint) faria a reentrega recriar o que
    alguém apagou de propósito. Aqui a exclusão fica de pé.
    """
    channel = inbox_a["channel"]
    wa_id = inbox_a["contact"].wa_id
    ingest_events(
        channel, parse_meta_webhook(build_inbound_payload(wa_id=wa_id, body="original"))
    )
    apagada = Message.objects.get(clinic=clinic_a, body="original")
    apagada.delete()

    ingest_events(
        channel, parse_meta_webhook(_payload_com_duas(wa_id, apagada.provider_message_id))
    )

    assert not Message.objects.filter(pk=apagada.pk).exists(), "continua fora da thread"
    assert Message.all_objects.filter(pk=apagada.pk).count() == 1, "e não virou duplicata"


def test_o_admin_apaga_mensagem_DE_VERDADE(clinic_a, inbox_a):
    """
    Fecha a porta de entrada do estado: sem isto o admin cria mensagens
    invisíveis que seguem ocupando o wamid.
    """
    from django.contrib.admin.sites import site

    from apps.inbox.admin import MessageAdmin

    channel = inbox_a["channel"]
    ingest_events(
        channel,
        parse_meta_webhook(
            build_inbound_payload(wa_id=inbox_a["contact"].wa_id, body="pelo admin")
        ),
    )
    mensagem = Message.objects.get(clinic=clinic_a, body="pelo admin")

    admin = MessageAdmin(Message, site)
    admin.delete_model(None, mensagem)

    assert not Message.all_objects.filter(pk=mensagem.pk).exists()


def test_o_admin_apaga_em_LOTE_de_verdade(clinic_a, inbox_a):
    """A ação "excluir selecionados" é o caminho mais usado da lista."""
    from django.contrib.admin.sites import site

    from apps.inbox.admin import MessageAdmin

    channel = inbox_a["channel"]
    for texto in ("uma", "outra"):
        ingest_events(
            channel,
            parse_meta_webhook(
                build_inbound_payload(wa_id=inbox_a["contact"].wa_id, body=texto)
            ),
        )

    admin = MessageAdmin(Message, site)
    admin.delete_queryset(None, Message.objects.filter(clinic=clinic_a))

    assert Message.all_objects.filter(clinic=clinic_a).count() == 0


# --------------------------------------------------------------------- #
# `unsupported` não é UM caso só (achado em 20/08/2026)
# --------------------------------------------------------------------- #
#
# A Meta usa o mesmo tipo para coisas muito diferentes. Os dois que chegaram de
# verdade na clínica, achados nos webhooks arquivados: `unknown`, que é a
# mensagem APAGADA pelo paciente, e `poll_creation`, que é enquete. Sem o
# subtipo, a tela dava a mesma frase para os dois e mandava a recepção abrir o
# celular à toa.


def _payload_unsupported(wa_id, subtipo, wamid="wamid.UNS-1"):
    """O formato REAL, copiado de um evento que a clínica recebeu."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "field": "messages",
                        "value": {
                            "metadata": {"phone_number_id": "PID"},
                            "contacts": [
                                {"wa_id": wa_id, "profile": {"name": "Marcia"}}
                            ],
                            "messages": [
                                {
                                    "id": wamid,
                                    "from": wa_id,
                                    "type": "unsupported",
                                    "timestamp": "1787169694",
                                    "errors": [
                                        {
                                            "code": 131051,
                                            "title": "Message type unknown",
                                            "error_data": {
                                                "details": "Message type is "
                                                "currently not supported."
                                            },
                                        }
                                    ],
                                    "unsupported": {"type": subtipo},
                                }
                            ],
                        },
                    }
                ]
            }
        ]
    }


def test_mensagem_apagada_pelo_paciente_guarda_o_subtipo(clinic_a, inbox_a):
    """
    ⚠️ A Meta NÃO diz qual mensagem foi apagada: não vem `context` nem o wamid
    da original. O que dá para fazer com honestidade é registrar o aviso na
    posição em que ela o entregou.
    """
    wa_id = inbox_a["contact"].wa_id
    ingest_events(
        inbox_a["channel"], parse_meta_webhook(_payload_unsupported(wa_id, "unknown"))
    )

    mensagem = Message.objects.get(clinic=clinic_a, provider_message_id="wamid.UNS-1")
    assert mensagem.kind == MessageKind.UNSUPPORTED
    assert mensagem.content_data["unsupported_type"] == "unknown"


def test_enquete_se_distingue_da_mensagem_apagada(clinic_a, inbox_a):
    """As duas chegam como `unsupported`; só o subtipo as separa."""
    wa_id = inbox_a["contact"].wa_id
    ingest_events(
        inbox_a["channel"],
        parse_meta_webhook(_payload_unsupported(wa_id, "poll_creation", "wamid.UNS-2")),
    )

    mensagem = Message.objects.get(clinic=clinic_a, provider_message_id="wamid.UNS-2")
    assert mensagem.content_data["unsupported_type"] == "poll_creation"


def test_unsupported_sem_subtipo_nao_quebra(clinic_a, inbox_a):
    """Tipo novo que a Meta invente: entra com o subtipo vazio e a tela usa a
    frase genérica, em vez de a ingestão estourar."""
    wa_id = inbox_a["contact"].wa_id
    payload = _payload_unsupported(wa_id, "", "wamid.UNS-3")
    payload["entry"][0]["changes"][0]["value"]["messages"][0].pop("unsupported")

    ingest_events(inbox_a["channel"], parse_meta_webhook(payload))

    mensagem = Message.objects.get(clinic=clinic_a, provider_message_id="wamid.UNS-3")
    assert mensagem.content_data["unsupported_type"] == ""
