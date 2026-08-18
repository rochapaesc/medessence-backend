"""
Opt-out de marketing (RF-SEQ-8), do webhook da Meta até a barreira do envio.

O pedido de silêncio é do NÚMERO, e vale venha o disparo de onde vier. Por isso
a barreira mora no `send_message`, o ponto onde todos os caminhos se encontram.
"""

from datetime import time

import pytest
from django.utils import timezone

from apps.automation.choices import EnrollmentSource, FlowStatus, SequenceEnrollmentStatus
from apps.automation.models import Sequence, SequenceStep
from apps.automation.sequences import inscrever
from apps.automation.tests.conftest import make_channel, make_contact, make_flow
from apps.inbox.choices import MessageKind, MessageStatus, SenderKind
from apps.inbox.models import Conversation, Message, WhatsAppTemplate
from apps.inbox.services import ingest_events, send_message
from apps.integrations.whatsapp.events import parse_meta_webhook

pytestmark = pytest.mark.django_db


def webhook_de_preferencia(wa_id, valor):
    """O formato que a Meta manda quando o contato usa o botão de parar."""
    return {
        "entry": [
            {
                "changes": [
                    {
                        "field": "user_preferences",
                        "value": {
                            "user_preferences": [
                                {
                                    "wa_id": wa_id,
                                    "detail": "User requested to stop marketing messages",
                                    "category": "marketing_messages",
                                    "value": valor,
                                    "timestamp": "1755100000",
                                }
                            ]
                        },
                    }
                ]
            }
        ]
    }


def trilha(clinic, *, marketing=True):
    sequence = Sequence.objects.create(
        clinic=clinic, name=f"Trilha {timezone.now().timestamp()}",
        is_active=True, is_marketing=marketing,
    )
    SequenceStep.objects.create(
        sequence=sequence,
        order=1,
        offset_days=0,
        send_time=time(8, 0),
        flow=make_flow(clinic, name=f"F{timezone.now().timestamp()}", status=FlowStatus.ACTIVE),
    )
    return sequence


# ---- o webhook ----


def test_parser_reconhece_a_preferencia_de_marketing(clinic_a):
    eventos = parse_meta_webhook(webhook_de_preferencia("5585900000001", "stop"))
    assert len(eventos) == 1
    assert eventos[0].marketing_opt_out is True

    eventos = parse_meta_webhook(webhook_de_preferencia("5585900000001", "resume"))
    assert eventos[0].marketing_opt_out is False


def test_parar_liga_o_opt_out_e_cancela_as_trilhas_de_marketing(clinic_a):
    channel = make_channel(clinic_a)
    contact = make_contact(clinic_a, wa_id="5585900000001")

    de_marketing = inscrever(trilha(clinic_a), contact, source=EnrollmentSource.BATCH)
    operacional = inscrever(
        trilha(clinic_a, marketing=False), contact, source=EnrollmentSource.BATCH
    )

    eventos = parse_meta_webhook(webhook_de_preferencia(contact.wa_id, "stop"))
    stats = ingest_events(channel, eventos)
    assert stats["preference"] == 1

    contact.refresh_from_db()
    de_marketing.refresh_from_db()
    operacional.refresh_from_db()

    assert contact.marketing_opt_out is True
    assert de_marketing.status == SequenceEnrollmentStatus.CANCELED
    # A operacional segue: parar promoção não é parar de confirmar consulta.
    assert operacional.status == SequenceEnrollmentStatus.ACTIVE


def test_voltar_atras_reabre_a_porta_sem_ressuscitar_trilha(clinic_a):
    channel = make_channel(clinic_a)
    contact = make_contact(clinic_a, wa_id="5585900000001")
    enrollment = inscrever(trilha(clinic_a), contact, source=EnrollmentSource.BATCH)

    ingest_events(channel, parse_meta_webhook(webhook_de_preferencia(contact.wa_id, "stop")))
    ingest_events(channel, parse_meta_webhook(webhook_de_preferencia(contact.wa_id, "resume")))

    contact.refresh_from_db()
    enrollment.refresh_from_db()
    assert contact.marketing_opt_out is False
    # Cancelada continua cancelada: ressuscitar seria decidir por quem não pediu.
    assert enrollment.status == SequenceEnrollmentStatus.CANCELED


def test_preferencia_repetida_nao_conta_duas_vezes(clinic_a):
    channel = make_channel(clinic_a)
    contact = make_contact(clinic_a, wa_id="5585900000001")

    ingest_events(channel, parse_meta_webhook(webhook_de_preferencia(contact.wa_id, "stop")))
    stats = ingest_events(
        channel, parse_meta_webhook(webhook_de_preferencia(contact.wa_id, "stop"))
    )
    assert stats["preference"] == 0


# ---- a barreira do envio ----


def _mensagem(clinic, contact, channel, *, kind, template_name=""):
    conversa = Conversation.objects.create(clinic=clinic, channel=channel, contact=contact)
    return Message.objects.create(
        clinic=clinic,
        conversation=conversa,
        kind=kind,
        body="Oi",
        template_name=template_name,
        sender_kind=SenderKind.BOT,
        wa_timestamp=timezone.now(),
    )


def test_template_de_marketing_nao_sai_para_quem_pediu_silencio(clinic_a):
    channel = make_channel(clinic_a)
    contact = make_contact(clinic_a)
    contact.marketing_opt_out = True
    contact.save(update_fields=["marketing_opt_out"])
    WhatsAppTemplate.objects.create(
        clinic=clinic_a, name="resgate", category="MARKETING", status="APPROVED"
    )

    message = _mensagem(
        clinic_a, contact, channel, kind=MessageKind.TEMPLATE, template_name="resgate"
    )
    send_message(message)

    message.refresh_from_db()
    assert message.status == MessageStatus.FAILED
    assert not message.provider_message_id
    assert "marketing" in message.status_error.lower()


def test_template_utilitario_continua_saindo(clinic_a):
    channel = make_channel(clinic_a)
    contact = make_contact(clinic_a)
    contact.marketing_opt_out = True
    contact.save(update_fields=["marketing_opt_out"])
    WhatsAppTemplate.objects.create(
        clinic=clinic_a, name="confirmacao", category="UTILITY", status="APPROVED"
    )

    message = _mensagem(
        clinic_a, contact, channel, kind=MessageKind.TEMPLATE, template_name="confirmacao"
    )
    send_message(message)

    message.refresh_from_db()
    assert message.status != MessageStatus.FAILED


def test_texto_livre_nao_e_barrado_pelo_opt_out(clinic_a):
    """
    Texto livre só existe dentro da janela de 24h, ou seja, numa conversa que o
    próprio paciente reabriu. Barrar ali seria a recepção sem conseguir
    responder quem escreveu.
    """
    channel = make_channel(clinic_a)
    contact = make_contact(clinic_a)
    contact.marketing_opt_out = True
    contact.save(update_fields=["marketing_opt_out"])

    message = _mensagem(clinic_a, contact, channel, kind=MessageKind.TEXT)
    send_message(message)

    message.refresh_from_db()
    assert message.status != MessageStatus.FAILED
