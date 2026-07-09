"""Endpoint de webhook (§7): 200 imediato, log cru, segredo por canal, envio."""

import uuid

from apps.inbox.models import Message, WebhookEvent
from apps.integrations.whatsapp.fake.adapter import build_inbound_payload


def _url(channel):
    return f"/webhooks/whatsapp/{channel.uuid}/{channel.webhook_secret}/"


def test_webhook_processa_e_responde_200(api_client, clinic_a, inbox_a):
    channel = inbox_a["channel"]
    payload = build_inbound_payload(wa_id="5585912345678", body="chegou pelo webhook")
    response = api_client.post(_url(channel), payload, format="json")
    assert response.status_code == 200
    # Log cru gravado e (task eager) ingestão concluída.
    assert WebhookEvent.objects.filter(clinic=clinic_a).count() == 1
    assert Message.objects.filter(clinic=clinic_a, body="chegou pelo webhook").exists()
    assert WebhookEvent.objects.get(clinic=clinic_a).processed_at is not None


def test_webhook_segredo_errado_404(api_client, inbox_a):
    channel = inbox_a["channel"]
    url = f"/webhooks/whatsapp/{channel.uuid}/segredo-errado/"
    response = api_client.post(url, {"entry": []}, format="json")
    assert response.status_code == 404
    assert WebhookEvent.objects.count() == 0


def test_webhook_canal_inexistente_404(api_client, db):
    url = f"/webhooks/whatsapp/{uuid.uuid4()}/qualquer/"
    response = api_client.post(url, {"entry": []}, format="json")
    assert response.status_code == 404


def test_webhook_get_nao_permitido(api_client, inbox_a):
    response = api_client.get(_url(inbox_a["channel"]))
    assert response.status_code == 405


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
