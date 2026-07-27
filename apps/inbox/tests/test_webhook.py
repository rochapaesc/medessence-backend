"""
Webhook Meta (§7): challenge no GET, HMAC no POST, roteamento por
phone_number_id e — acima de tudo — nenhum evento na clínica errada.
"""

import hashlib
import hmac
import json

from django.conf import settings

from apps.inbox.choices import WebhookSource
from apps.inbox.models import Channel, Message, WebhookEvent
from apps.integrations.whatsapp.fake.adapter import build_inbound_payload

URL = "/webhooks/whatsapp/meta/"
PHONE_A = "111000000000001"
PHONE_B = "222000000000002"


def _sign(body: bytes) -> str:
    digest = hmac.new(
        settings.WHATSAPP_APP_SECRET.encode(), body, hashlib.sha256
    ).hexdigest()
    return f"sha256={digest}"


def _post_signed(api_client, payload: dict, signature: str | None = None):
    body = json.dumps(payload).encode()
    return api_client.post(
        URL,
        data=body,
        content_type="application/json",
        HTTP_X_HUB_SIGNATURE_256=signature if signature is not None else _sign(body),
    )


def _wire_channel(channel, phone_number_id):
    channel.phone_number_id = phone_number_id
    channel.save(update_fields=["phone_number_id"])
    return channel


# ------------------------------ verificação ------------------------------ #


def test_challenge_com_verify_token_certo(api_client, db):
    response = api_client.get(
        URL,
        {
            "hub.mode": "subscribe",
            "hub.verify_token": settings.WHATSAPP_VERIFY_TOKEN,
            "hub.challenge": "12345",
        },
    )
    assert response.status_code == 200
    assert response.content == b"12345"


def test_challenge_com_token_errado_403(api_client, db):
    response = api_client.get(
        URL,
        {"hub.mode": "subscribe", "hub.verify_token": "errado", "hub.challenge": "x"},
    )
    assert response.status_code == 403


# ------------------------------- assinatura ------------------------------ #


def test_post_assinado_processa_e_responde_200(api_client, clinic_a, inbox_a):
    _wire_channel(inbox_a["channel"], PHONE_A)
    payload = build_inbound_payload(
        wa_id="5585912345678", body="chegou pelo webhook", phone_number_id=PHONE_A
    )

    response = _post_signed(api_client, payload)

    assert response.status_code == 200
    event = WebhookEvent.objects.get(clinic=clinic_a)
    assert event.source == WebhookSource.META
    assert event.processed_at is not None, "task eager concluiu a ingestão"
    assert Message.objects.filter(clinic=clinic_a, body="chegou pelo webhook").exists()


def test_post_com_assinatura_errada_403_e_nada_gravado(api_client, inbox_a):
    _wire_channel(inbox_a["channel"], PHONE_A)
    payload = build_inbound_payload(wa_id="1", body="x", phone_number_id=PHONE_A)

    response = _post_signed(api_client, payload, signature="sha256=" + "0" * 64)

    assert response.status_code == 403
    assert WebhookEvent.objects.count() == 0


def test_post_sem_assinatura_403(api_client, inbox_a):
    response = api_client.post(URL, {"entry": []}, format="json")
    assert response.status_code == 403


def test_sem_app_secret_configurado_fecha_tudo(api_client, inbox_a, settings):
    """Fail closed: plataforma sem app secret não aceita webhook nenhum."""
    settings.WHATSAPP_APP_SECRET = ""
    response = _post_signed(api_client, {"entry": []}, signature="sha256=" + "0" * 64)
    assert response.status_code == 403


# ------------------------------- roteamento ------------------------------ #


def test_numero_desconhecido_200_com_rastro_global(api_client, db):
    """200 mesmo sem canal — não-2xx viraria retry infinito da Meta. O rastro
    fica num evento global (clinic nula) com o motivo."""
    payload = build_inbound_payload(
        wa_id="5585912345678", body="perdido", phone_number_id="999999999999999"
    )

    response = _post_signed(api_client, payload)

    assert response.status_code == 200
    event = WebhookEvent.objects.get()
    assert event.clinic is None
    assert "999999999999999" in event.error
    assert Message.objects.count() == 0


def test_payload_sem_numero_200_com_rastro_global(api_client, db):
    """Update de conta/template (sem metadata) — guarda o cru e segue."""
    response = _post_signed(api_client, {"entry": [{"changes": [{"value": {}}]}]})

    assert response.status_code == 200
    event = WebhookEvent.objects.get()
    assert event.clinic is None
    assert event.error == ""


def test_payload_multi_numero_nao_vaza_entre_clinicas(
    api_client, clinic_a, clinic_b, inbox_a, inbox_b
):
    """O teste que importa: cada clínica arquiva e processa SÓ o que é dela."""
    _wire_channel(inbox_a["channel"], PHONE_A)
    _wire_channel(inbox_b["channel"], PHONE_B)
    payload_a = build_inbound_payload(
        wa_id="5585911110001", body="para a clínica A", phone_number_id=PHONE_A
    )
    payload_b = build_inbound_payload(
        wa_id="5585911110002", body="para a clínica B", phone_number_id=PHONE_B
    )
    combined = {"entry": payload_a["entry"] + payload_b["entry"]}

    response = _post_signed(api_client, combined)

    assert response.status_code == 200
    assert Message.objects.filter(clinic=clinic_a, body="para a clínica A").exists()
    assert Message.objects.filter(clinic=clinic_b, body="para a clínica B").exists()
    assert not Message.objects.filter(clinic=clinic_a, body="para a clínica B").exists()
    assert not Message.objects.filter(clinic=clinic_b, body="para a clínica A").exists()
    # E o payload ARQUIVADO de cada clínica também é só dela.
    event_a = WebhookEvent.objects.get(clinic=clinic_a)
    assert "para a clínica B" not in json.dumps(event_a.payload)


def test_metodo_nao_permitido(api_client, db):
    assert api_client.put(URL, {}, format="json").status_code == 405


# --------------------------------- envio --------------------------------- #


def test_criar_mensagem_envia_via_provider_fake(api_client, manager_single_clinic, inbox_a):
    from apps.inbox.choices import SenderKind
    from apps.inbox.tests.conftest import make_message

    conversation = inbox_a["conversation"]
    make_message(conversation, sender_kind=SenderKind.CONTACT)  # abre a janela
    api_client.force_authenticate(manager_single_clinic)
    response = api_client.post(
        "/api/v1/messages/",
        {"conversation": conversation.id, "body": "resposta"},
        format="json",
    )
    assert response.status_code == 201
    # Task eager de envio já gravou o wamid (FAKE) e o status.
    message = Message.objects.get(pk=response.data["id"])
    assert message.provider_message_id.startswith("wamid.FAKE-")
    assert message.status == "sent"
