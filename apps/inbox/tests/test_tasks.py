"""Tasks do inbox: envio idempotente, refresh de templates, re-hosting de mídia,
adapter Meta sem credenciais."""

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
    from apps.integrations.whatsapp.base import DownloadedMedia
    from apps.integrations.whatsapp.fake.adapter import FakeWhatsAppAdapter

    media = MediaAsset.objects.create(clinic=clinic_a, provider_media_id="m1")

    monkeypatch.setattr(
        FakeWhatsAppAdapter,
        "download_media",
        lambda self, media_id: DownloadedMedia(
            content=b"\x89PNG-bytes", mime_type="image/png"
        ),
    )

    inbox_tasks.fetch_media_asset(media.pk)
    media.refresh_from_db()
    assert media.stored_file.name
    assert media.size_bytes == len(b"\x89PNG-bytes")


def test_fetch_media_skip_sem_conteudo(clinic_a, inbox_a):
    """O FAKE devolve content vazio - o fetch pula em vez de gravar arquivo oco."""
    from apps.inbox.tasks import fetch_media_asset

    media = MediaAsset.objects.create(clinic=clinic_a, provider_media_id="m2")
    assert fetch_media_asset(media.pk) == "skipped: sem conteúdo"


def test_adapter_meta_sem_credenciais(clinic_a):
    from apps.integrations.whatsapp.exceptions import WhatsAppNotConfiguredError
    from apps.integrations.whatsapp.registry import get_whatsapp_provider

    channel = Channel.objects.create(clinic=clinic_a, provider=WhatsAppProviderKind.META)
    with pytest.raises(WhatsAppNotConfiguredError):
        get_whatsapp_provider(channel)
