"""Tasks do inbox: envio idempotente, refresh de templates, re-hosting de mídia,
client Datafy sem credenciais."""

import pytest

from apps.inbox.choices import SenderKind, WhatsAppProviderKind
from apps.inbox.models import Channel, MediaAsset, WhatsAppTemplate
from apps.inbox.tests.conftest import make_message


def test_send_skip_se_ja_enviada(inbox_a):
    from apps.inbox.tasks import send_whatsapp_message

    message = make_message(
        inbox_a["conversation"], sender_kind=SenderKind.AGENT, mid="wamid.already"
    )
    assert send_whatsapp_message(message.pk) == "skipped: já enviada"


def test_refresh_channel_templates(clinic_a, inbox_a):
    from apps.inbox.tasks import refresh_channel_templates

    refresh_channel_templates(inbox_a["channel"].pk)
    assert WhatsAppTemplate.objects.filter(
        clinic=clinic_a, name="confirmacao_consulta", status="APPROVED"
    ).exists()


def test_fetch_media_asset_rehospeda(monkeypatch, clinic_a, inbox_a):
    from apps.inbox import tasks as inbox_tasks
    from apps.integrations.whatsapp.base import MediaURL
    from apps.integrations.whatsapp.fake.adapter import FakeWhatsAppAdapter

    media = MediaAsset.objects.create(clinic=clinic_a, provider_media_id="m1")

    monkeypatch.setattr(
        FakeWhatsAppAdapter,
        "resolve_media",
        lambda self, media_id: MediaURL(url="https://x/y.png", mime_type="image/png"),
    )

    class _Resp:
        content = b"\x89PNG-bytes"

        def raise_for_status(self):
            return None

    monkeypatch.setattr(inbox_tasks.requests, "get", lambda *a, **k: _Resp())

    inbox_tasks.fetch_media_asset(media.pk)
    media.refresh_from_db()
    assert media.stored_file.name
    assert media.size_bytes == len(b"\x89PNG-bytes")


def test_fetch_media_skip_sem_url(clinic_a, inbox_a):
    from apps.inbox.tasks import fetch_media_asset

    media = MediaAsset.objects.create(clinic=clinic_a, provider_media_id="m2")
    assert fetch_media_asset(media.pk) == "skipped: sem URL"


def test_datafy_client_sem_credenciais(clinic_a):
    from apps.integrations.whatsapp.datafy.client import DatafyClient
    from apps.integrations.whatsapp.exceptions import WhatsAppNotConfiguredError

    channel = Channel.objects.create(clinic=clinic_a, provider=WhatsAppProviderKind.DATAFY)
    with pytest.raises(WhatsAppNotConfiguredError):
        DatafyClient(channel)
