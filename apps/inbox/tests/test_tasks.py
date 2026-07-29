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


def test_fetch_media_sem_conteudo_marca_falha(clinic_a, inbox_a):
    """O FAKE devolve content vazio: não grava arquivo oco E não sai calado.

    Saía como 'skipped' e a mídia ficava para sempre em 'Baixando…' na tela —
    a mesma coisa que a mídia cuja URL expirou na Meta.
    """
    from apps.inbox.choices import MediaState
    from apps.inbox.tasks import fetch_media_asset

    media = MediaAsset.objects.create(clinic=clinic_a, provider_media_id="m2")

    resultado = fetch_media_asset(media.pk)

    media.refresh_from_db()
    assert resultado.startswith("failed:")
    assert media.state == MediaState.FAILED
    assert not media.stored_file.name


def test_envio_com_erro_de_negocio_vira_failed_com_motivo(monkeypatch, inbox_a):
    """Janela fechada, número inválido etc.: retry não resolve. Antes desta
    captura a mensagem ficava pendente para sempre, sem explicação."""
    from apps.inbox.choices import MessageStatus
    from apps.inbox.services import send_message
    from apps.integrations.whatsapp.exceptions import WhatsAppError
    from apps.integrations.whatsapp.fake.adapter import FakeWhatsAppAdapter

    def _janela_fechada(self, to, body, reply_to=None):
        raise WhatsAppError("131047 Re-engagement message: janela de 24h fechada")

    monkeypatch.setattr(FakeWhatsAppAdapter, "send_text", _janela_fechada)
    message = make_message(inbox_a["conversation"], sender_kind=SenderKind.AGENT)

    send_message(message)

    message.refresh_from_db()
    assert message.status == MessageStatus.FAILED
    assert "131047" in message.status_error
    assert message.provider_message_id == "", "não ganhou wamid — nunca saiu"


def test_erro_transitorio_no_envio_sobe_para_o_retry(monkeypatch, inbox_a):
    """Rate limit NÃO vira failed: propaga para o autoretry da task."""
    import pytest as _pytest

    from apps.inbox.services import send_message
    from apps.integrations.whatsapp.exceptions import WhatsAppRateLimitedError
    from apps.integrations.whatsapp.fake.adapter import FakeWhatsAppAdapter

    def _rate_limited(self, to, body, reply_to=None):
        raise WhatsAppRateLimitedError("429")

    monkeypatch.setattr(FakeWhatsAppAdapter, "send_text", _rate_limited)
    message = make_message(inbox_a["conversation"], sender_kind=SenderKind.AGENT)

    with _pytest.raises(WhatsAppRateLimitedError):
        send_message(message)

    message.refresh_from_db()
    assert message.status == "", "continua pendente — a fila tenta de novo"


def test_adapter_meta_sem_credenciais(clinic_a):
    from apps.integrations.whatsapp.exceptions import WhatsAppNotConfiguredError
    from apps.integrations.whatsapp.registry import get_whatsapp_provider

    channel = Channel.objects.create(clinic=clinic_a, provider=WhatsAppProviderKind.META)
    with pytest.raises(WhatsAppNotConfiguredError):
        get_whatsapp_provider(channel)
