"""
Adapter Meta Cloud API (§7) - PyWa como SDK de transporte, nunca framework.

O modelo "bot" do PyWa (client global, decorators, servidor embutido) não
serve ao multi-tenant: aqui o client é instanciado POR CANAL, sob demanda, e
morre com o adapter. O extra `server` do pacote nem é instalado - webhook é
view DRF nossa, e o parse continua no parser Meta próprio (`events.py`), que
já falava este formato quando o transporte era o proxy.
"""

from pywa import WhatsApp
from pywa import errors as pywa_errors
from pywa.types.templates import TemplateLanguage

from apps.integrations.whatsapp.base import (
    DownloadedMedia,
    SendResult,
    Template,
    WhatsAppEvent,
)
from apps.integrations.whatsapp.events import parse_meta_webhook
from apps.integrations.whatsapp.exceptions import (
    WhatsAppAuthError,
    WhatsAppError,
    WhatsAppNotConfiguredError,
    WhatsAppRateLimitedError,
    WhatsAppUnavailableError,
)

# Página do GET /{waba_id}/message_templates - teto da Meta é 100.
TEMPLATES_PAGE_SIZE = 100


def _translate(exc: pywa_errors.WhatsAppError) -> WhatsAppError:
    """
    pywa.errors → nossas exceções: o resto do sistema (retry das tasks,
    mensagens ao usuário) não conhece PyWa. A ordem importa - as classes
    específicas descendem das genéricas.
    """
    if isinstance(exc, pywa_errors.AuthorizationError):
        return WhatsAppAuthError(str(exc))
    if isinstance(exc, pywa_errors.ThrottlingError):
        return WhatsAppRateLimitedError(str(exc))
    if isinstance(exc, pywa_errors.ServiceUnavailable):
        return WhatsAppUnavailableError(str(exc))
    # Erros de negócio (janela fechada, número fora da allowed list, template
    # rejeitado...): sem retry - a mensagem vira FAILED com o motivo.
    return WhatsAppError(str(exc))


class MetaAdapter:
    def __init__(self, channel):
        credentials = channel.credentials or {}
        token = credentials.get("access_token", "")
        phone_id = channel.phone_number_id or credentials.get("phone_number_id", "")
        if not token or not phone_id:
            raise WhatsAppNotConfiguredError(
                "Canal Meta sem access_token/phone_number_id configurados."
            )
        self.channel = channel
        self.waba_id = channel.waba_id or credentials.get("waba_id", "")
        self._wa = WhatsApp(
            phone_id=phone_id,
            token=token,
            waba_id=self.waba_id or None,
            # A assinatura HMAC do webhook é conferida na NOSSA view (§7);
            # aqui o client só fala PARA FORA.
            validate_updates=False,
        )

    # ------------------------------- envio ------------------------------- #

    def send_text(self, to: str, body: str, reply_to: str | None = None) -> SendResult:
        try:
            sent = self._wa.send_message(
                to=to, text=body, reply_to_message_id=reply_to or None
            )
        except pywa_errors.WhatsAppError as exc:
            raise _translate(exc) from exc
        return SendResult(provider_message_id=sent.id, raw={"id": sent.id})

    def send_template(
        self, to: str, name: str, language: str, components: list | None = None
    ) -> SendResult:
        # PyWa exige o enum (faz `language.value` internamente) - passar a
        # string crua estoura com AttributeError na hora do envio real.
        # Descoberto na calibração de 28/07/2026: o dublê dos testes aceitava
        # qualquer coisa, então só apareceu contra a Meta de verdade.
        # Idioma que o PyWa não conhece NÃO levanta: vira `UNKNOWN` com um
        # warning, e o envio falharia lá na frente com erro genérico da Meta.
        # Melhor barrar aqui, com o nome do idioma na mensagem.
        idioma = TemplateLanguage(language)
        if idioma == TemplateLanguage.UNKNOWN:
            raise WhatsAppError(
                f"Idioma de template desconhecido: {language!r}. "
                "Use o código que a Meta devolve no template (ex.: pt_BR, en_US)."
            )

        try:
            sent = self._wa.send_template(
                to=to,
                name=name,
                language=idioma,
                # PyWa aceita dicts crus na lista - os params ficam no formato
                # Meta que o nosso cache de templates já guarda.
                params=components or None,
            )
        except pywa_errors.WhatsAppError as exc:
            raise _translate(exc) from exc
        return SendResult(provider_message_id=sent.id, raw={"id": sent.id})

    def send_media(
        self, to: str, kind: str, url_or_id: str, caption: str | None = None
    ) -> SendResult:
        senders = {
            "image": lambda: self._wa.send_image(to=to, image=url_or_id, caption=caption),
            "video": lambda: self._wa.send_video(to=to, video=url_or_id, caption=caption),
            "audio": lambda: self._wa.send_audio(to=to, audio=url_or_id),
            "document": lambda: self._wa.send_document(
                to=to, document=url_or_id, caption=caption
            ),
        }
        sender = senders.get(kind, senders["document"])
        try:
            sent = sender()
        except pywa_errors.WhatsAppError as exc:
            raise _translate(exc) from exc
        return SendResult(provider_message_id=sent.id, raw={"id": sent.id})

    # ----------------------------- leitura ------------------------------- #

    def mark_read(self, provider_message_id: str) -> None:
        try:
            self._wa.mark_message_as_read(message_id=provider_message_id)
        except pywa_errors.WhatsAppError as exc:
            raise _translate(exc) from exc

    def download_media(self, media_id: str) -> DownloadedMedia:
        """URL efêmera (~5 min) + download autenticado, numa tacada só."""
        try:
            url_info = self._wa.get_media_url(media_id)
            content = self._wa.get_media_bytes(url=url_info.url)
        except pywa_errors.WhatsAppError as exc:
            raise _translate(exc) from exc
        return DownloadedMedia(content=content, mime_type=url_info.mime_type or "")

    def list_templates(self) -> list[Template]:
        """
        Cache de templates (beat 6h). Usa a camada CRUA do PyWa
        (`api.get_templates` → JSON Graph): `components` precisa chegar no
        formato Meta original para o JSONField do WhatsAppTemplate - os tipos
        do PyWa embutem referência ao client e não sobrevivem a asdict().
        """
        if not self.waba_id:
            raise WhatsAppNotConfiguredError("Canal Meta sem waba_id - templates ficam sem cache.")

        templates: list[Template] = []
        pagination: dict | None = {"limit": TEMPLATES_PAGE_SIZE}
        while pagination is not None:
            try:
                page = self._wa.api.get_templates(
                    waba_id=self.waba_id, pagination=pagination
                )
            except pywa_errors.WhatsAppError as exc:
                raise _translate(exc) from exc
            for item in page.get("data", []):
                templates.append(
                    Template(
                        name=item.get("name", ""),
                        language=item.get("language", ""),
                        category=item.get("category", ""),
                        status=item.get("status", ""),
                        components=item.get("components", []),
                    )
                )
            after = page.get("paging", {}).get("cursors", {}).get("after")
            has_next = bool(page.get("paging", {}).get("next"))
            pagination = (
                {"limit": TEMPLATES_PAGE_SIZE, "after": after}
                if has_next and after
                else None
            )
        return templates

    # ----------------------------- webhook ------------------------------- #

    def parse_webhook(self, payload: dict) -> list[WhatsAppEvent]:
        return parse_meta_webhook(payload)
