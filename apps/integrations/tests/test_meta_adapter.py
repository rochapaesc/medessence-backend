"""
Contrato do MetaAdapter (§7) - PyWa mockado no objeto, nunca HTTP real.

O que se protege aqui é a TRADUÇÃO: porta de entrada/saída no nosso formato,
PyWa preso dentro do adapter. Se o PyWa mudar de API, estes testes quebram
no upgrade - e não em produção no meio de um envio.
"""

from types import SimpleNamespace

import pytest
from pywa import errors as pywa_errors

from apps.inbox.choices import WhatsAppProviderKind
from apps.inbox.models import Channel
from apps.integrations.whatsapp.base import WhatsAppEventKind, WhatsAppProvider
from apps.integrations.whatsapp.exceptions import (
    WhatsAppAuthError,
    WhatsAppError,
    WhatsAppNotConfiguredError,
    WhatsAppRateLimitedError,
)
from apps.integrations.whatsapp.fake.adapter import build_inbound_payload
from apps.integrations.whatsapp.meta.adapter import MetaAdapter


@pytest.fixture
def channel(db, clinic_a):
    return Channel.objects.create(
        clinic=clinic_a,
        provider=WhatsAppProviderKind.META,
        phone_number_id="111222333444555",
        waba_id="999888777666",
        credentials={"access_token": "token-de-teste"},
    )


class _StubWa:
    """Dublê do client PyWa: grava a chamada e devolve o combinado."""

    def __init__(self):
        self.calls = []
        self.api = SimpleNamespace(get_templates=self._get_templates)
        self.templates_pages = [
            {
                "data": [
                    {
                        "name": "confirmacao_consulta",
                        "language": "pt_BR",
                        "category": "UTILITY",
                        "status": "APPROVED",
                        "components": [{"type": "BODY", "text": "Olá {{1}}"}],
                    }
                ],
                "paging": {"cursors": {"after": ""}},
            }
        ]
        self.raises: Exception | None = None

    def _record(self, _method, kwargs):
        if self.raises is not None:
            raise self.raises
        self.calls.append((_method, kwargs))
        return SimpleNamespace(id="wamid.META-1")

    def send_message(self, **kwargs):
        return self._record("send_message", kwargs)

    def send_template(self, **kwargs):
        return self._record("send_template", kwargs)

    def send_image(self, **kwargs):
        return self._record("send_image", kwargs)

    def send_document(self, **kwargs):
        return self._record("send_document", kwargs)

    def mark_message_as_read(self, **kwargs):
        self._record("mark_read", kwargs)

    def get_media_url(self, media_id):
        self.calls.append(("get_media_url", {"media_id": media_id}))
        return SimpleNamespace(url="https://meta/efemera", mime_type="image/png")

    def get_media_bytes(self, *, url, **kwargs):
        self.calls.append(("get_media_bytes", {"url": url}))
        return b"png-bytes"

    def _get_templates(self, waba_id, pagination=None, **kwargs):
        self.calls.append(("get_templates", {"waba_id": waba_id, "pagination": pagination}))
        return self.templates_pages.pop(0)


@pytest.fixture
def adapter(channel):
    instance = MetaAdapter(channel)
    instance._wa = _StubWa()
    return instance


def test_cumpre_o_port(adapter):
    assert isinstance(adapter, WhatsAppProvider)


def test_sem_credenciais_recusa_cedo(db, clinic_a):
    channel = Channel.objects.create(clinic=clinic_a, provider=WhatsAppProviderKind.META)
    with pytest.raises(WhatsAppNotConfiguredError):
        MetaAdapter(channel)


def test_send_text_traduz_ida_e_volta(adapter):
    result = adapter.send_text("5585912345678", "olá", reply_to="wamid.orig")

    name, kwargs = adapter._wa.calls[0]
    assert name == "send_message"
    assert kwargs == {
        "to": "5585912345678",
        "text": "olá",
        "reply_to_message_id": "wamid.orig",
    }
    assert result.provider_message_id == "wamid.META-1"


def test_send_template_passa_components_crus(adapter):
    components = [{"type": "body", "parameters": [{"type": "text", "text": "Ana"}]}]
    adapter.send_template("5585912345678", "confirmacao", "pt_BR", components)

    name, kwargs = adapter._wa.calls[0]
    assert name == "send_template"
    assert kwargs["name"] == "confirmacao"
    assert kwargs["params"] == components


def test_send_template_converte_o_idioma_para_o_enum(adapter):
    """
    O PyWa faz `language.value` internamente: string crua estoura com
    AttributeError NO ENVIO REAL. O dublê aceita qualquer coisa, então em
    28/07/2026 isso só apareceu contra a Meta — este teste fecha a brecha.
    """
    from pywa.types.templates import TemplateLanguage

    adapter.send_template("5585912345678", "hello_world", "en_US")

    _, kwargs = adapter._wa.calls[0]
    assert isinstance(kwargs["language"], TemplateLanguage)
    assert kwargs["language"].value == "en_US"


def test_idioma_invalido_vira_erro_nosso_e_nao_ValueError(adapter):
    with pytest.raises(WhatsAppError, match="Idioma de template desconhecido"):
        adapter.send_template("5585912345678", "x", "klingon")


def test_send_media_mapeia_kind(adapter):
    adapter.send_media("5585912345678", "image", "media-id-1", caption="legenda")
    assert adapter._wa.calls[0][0] == "send_image"

    adapter._wa.calls.clear()
    adapter.send_media("5585912345678", "arquivo-desconhecido", "media-id-2")
    assert adapter._wa.calls[0][0] == "send_document", "kind estranho cai em documento"


def test_download_media_resolve_e_baixa_autenticado(adapter):
    media = adapter.download_media("media-77")

    assert [c[0] for c in adapter._wa.calls] == ["get_media_url", "get_media_bytes"]
    assert media.content == b"png-bytes"
    assert media.mime_type == "image/png"


def test_list_templates_normaliza_o_json_cru(adapter):
    templates = adapter.list_templates()

    assert len(templates) == 1
    template = templates[0]
    assert template.name == "confirmacao_consulta"
    assert template.status == "APPROVED"
    # components chegam no formato Meta cru - é o que o JSONField guarda.
    assert template.components == [{"type": "BODY", "text": "Olá {{1}}"}]


def test_list_templates_pagina_ate_o_fim(adapter):
    adapter._wa.templates_pages = [
        {
            "data": [{"name": "t1", "language": "pt_BR"}],
            "paging": {"cursors": {"after": "CURSOR"}, "next": "https://graph/..."},
        },
        {"data": [{"name": "t2", "language": "pt_BR"}], "paging": {}},
    ]

    templates = adapter.list_templates()

    assert [t.name for t in templates] == ["t1", "t2"]
    segunda_chamada = adapter._wa.calls[1][1]
    assert segunda_chamada["pagination"]["after"] == "CURSOR"


def test_sem_waba_id_templates_recusam(db, clinic_a):
    channel = Channel.objects.create(
        clinic=clinic_a,
        provider=WhatsAppProviderKind.META,
        phone_number_id="111",
        credentials={"access_token": "t"},
    )
    adapter = MetaAdapter(channel)
    with pytest.raises(WhatsAppNotConfiguredError):
        adapter.list_templates()


def test_parse_webhook_delega_ao_parser_meta(adapter):
    events = adapter.parse_webhook(
        build_inbound_payload(wa_id="5585912345678", body="oi")
    )

    assert len(events) == 1
    assert events[0].kind == WhatsAppEventKind.INBOUND
    assert events[0].body == "oi"


def test_erros_do_pywa_viram_os_nossos(adapter):
    """O retry das tasks e o FAILED da mensagem dependem desta tradução."""
    cases = [
        (pywa_errors.AuthorizationError, WhatsAppAuthError),
        (pywa_errors.ThrottlingError, WhatsAppRateLimitedError),
        (pywa_errors.SendMessageError, WhatsAppError),
    ]
    for pywa_class, our_class in cases:
        adapter._wa.raises = pywa_class(raw={}, code=0, message="erro de teste")
        with pytest.raises(our_class):
            adapter.send_text("5585912345678", "x")
