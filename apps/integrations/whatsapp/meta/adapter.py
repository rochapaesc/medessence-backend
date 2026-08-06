"""
Adapter Meta Cloud API (§7) - PyWa como SDK de transporte, nunca framework.

O modelo "bot" do PyWa (client global, decorators, servidor embutido) não
serve ao multi-tenant: aqui o client é instanciado POR CANAL, sob demanda, e
morre com o adapter. O extra `server` do pacote nem é instalado - webhook é
view DRF nossa, e o parse continua no parser Meta próprio (`events.py`), que
já falava este formato quando o transporte era o proxy.
"""

import httpx
from pywa import WhatsApp
from pywa import errors as pywa_errors
from pywa import types as pywa_types
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


def _rede(exc: Exception) -> WhatsAppUnavailableError:
    """
    Timeout/queda de conexão com a Meta -> transitório (a task re-tenta).

    Achado ao vivo no fechamento (28/07): um `httpx.ConnectTimeout` vazou CRU
    pelo adapter - o autoretry só conhece as NOSSAS exceções, então não
    disparou e a mensagem ficou pendente para sempre, sem erro na tela. A
    mesma classe de defeito que já tinha sido morta para erros de negócio.
    """
    return WhatsAppUnavailableError(f"Falha de rede com a Meta: {exc}")


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
            sent = self._wa.send_message(to=to, text=body, reply_to_message_id=reply_to or None)
        except pywa_errors.WhatsAppError as exc:
            raise _translate(exc) from exc
        except httpx.TransportError as exc:
            raise _rede(exc) from exc
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
        except httpx.TransportError as exc:
            raise _rede(exc) from exc
        return SendResult(provider_message_id=sent.id, raw={"id": sent.id})

    def send_buttons(self, to: str, body: str, buttons: list[dict]) -> SendResult:
        """
        Botões de resposta rápida (F2.6).

        `callback_data` do PyWa é o que volta em `interactive.button_reply.id`
        no webhook - é o identificador estável pelo qual o motor de fluxos
        resolve a aresta. O TÍTULO não serve: o gestor reescreve "Marcar
        consulta" para "Agendar" e todas as ligações do fluxo se perderiam.
        """
        try:
            sent = self._wa.send_message(
                to=to,
                text=body,
                buttons=[
                    pywa_types.Button(title=b["title"], callback_data=b["id"]) for b in buttons
                ],
            )
        except pywa_errors.WhatsAppError as exc:
            raise _translate(exc) from exc
        except httpx.TransportError as exc:
            raise _rede(exc) from exc
        return SendResult(provider_message_id=sent.id, raw={"id": sent.id})

    def send_list(self, to: str, body: str, button_label: str, sections: list[dict]) -> SendResult:
        """Lista suspensa (F2.6). Mesmo raciocínio do `callback_data` acima."""
        try:
            sent = self._wa.send_message(
                to=to,
                text=body,
                buttons=pywa_types.SectionList(
                    button_title=button_label,
                    sections=[
                        pywa_types.Section(
                            title=s.get("title") or "",
                            rows=[
                                pywa_types.SectionRow(
                                    title=r["title"],
                                    callback_data=r["id"],
                                    description=r.get("description") or "",
                                )
                                for r in s.get("rows") or []
                            ],
                        )
                        for s in sections
                    ],
                ),
            )
        except pywa_errors.WhatsAppError as exc:
            raise _translate(exc) from exc
        except httpx.TransportError as exc:
            raise _rede(exc) from exc
        return SendResult(provider_message_id=sent.id, raw={"id": sent.id})

    def send_media(
        self,
        to: str,
        kind: str,
        url_or_id: str,
        caption: str | None = None,
        *,
        filename: str | None = None,
        mime_type: str | None = None,
        reply_to: str | None = None,
        is_voice: bool = False,
    ) -> SendResult:
        """
        Manda o anexo. `url_or_id` aceita CAMINHO LOCAL, URL ou media id.

        Passamos o caminho do arquivo no disco: o PyWa detecta que é caminho,
        sobe o arquivo para a Meta e usa o media id resultante. O caminho da
        URL exigiria que o nosso storage fosse alcançável pela internet — o
        que não vale nem em desenvolvimento (localhost) nem em produção, onde
        a mídia da clínica não deve ficar pública para quem tiver o link.
        """
        reply = reply_to or None
        senders = {
            "image": lambda: self._wa.send_image(
                to=to,
                image=url_or_id,
                caption=caption,
                mime_type=mime_type,
                reply_to_message_id=reply,
            ),
            "video": lambda: self._wa.send_video(
                to=to,
                video=url_or_id,
                caption=caption,
                mime_type=mime_type,
                reply_to_message_id=reply,
            ),
            # `is_voice` é o que faz o balão chegar como NOTA DE VOZ no celular
            # do paciente — com onda e play — em vez de anexo de áudio.
            "audio": lambda: self._wa.send_audio(
                to=to,
                audio=url_or_id,
                is_voice=is_voice,
                mime_type=mime_type,
                reply_to_message_id=reply,
            ),
            "sticker": lambda: self._wa.send_sticker(
                to=to,
                sticker=url_or_id,
                mime_type=mime_type,
                reply_to_message_id=reply,
            ),
            "document": lambda: self._wa.send_document(
                to=to,
                document=url_or_id,
                caption=caption,
                # Sem o nome, o paciente recebe "3f8a...bin" e não sabe se
                # abre o laudo ou o preparo.
                filename=filename,
                mime_type=mime_type,
                reply_to_message_id=reply,
            ),
        }
        sender = senders.get(kind, senders["document"])
        try:
            sent = sender()
        except pywa_errors.WhatsAppError as exc:
            raise _translate(exc) from exc
        except httpx.TransportError as exc:
            raise _rede(exc) from exc
        return SendResult(provider_message_id=sent.id, raw={"id": sent.id})

    def send_reaction(self, to: str, provider_message_id: str, emoji: str) -> SendResult:
        """
        Selo na mensagem. Emoji vazio TIRA — é o que a Meta manda no mesmo
        evento, e é por isso que desfazer não tem endpoint próprio.
        """
        try:
            if emoji:
                sent = self._wa.send_reaction(to=to, emoji=emoji, message_id=provider_message_id)
            else:
                sent = self._wa.remove_reaction(to=to, message_id=provider_message_id)
        except pywa_errors.WhatsAppError as exc:
            raise _translate(exc) from exc
        except httpx.TransportError as exc:
            raise _rede(exc) from exc
        return SendResult(provider_message_id=sent.id, raw={"id": sent.id})

    # ----------------------------- leitura ------------------------------- #

    def mark_read(self, provider_message_id: str) -> None:
        try:
            self._wa.mark_message_as_read(message_id=provider_message_id)
        except pywa_errors.WhatsAppError as exc:
            raise _translate(exc) from exc
        except httpx.TransportError as exc:
            raise _rede(exc) from exc

    def download_media(self, media_id: str) -> DownloadedMedia:
        """URL efêmera (~5 min) + download autenticado, numa tacada só."""
        try:
            url_info = self._wa.get_media_url(media_id)
            content = self._wa.get_media_bytes(url=url_info.url)
        except pywa_errors.WhatsAppError as exc:
            raise _translate(exc) from exc
        except httpx.TransportError as exc:
            raise _rede(exc) from exc
        return DownloadedMedia(content=content, mime_type=url_info.mime_type or "")

    def verify_credentials(self) -> dict:
        """
        Uma chamada real ao Graph só para saber se a credencial serve.

        É a PORTA DE SAÍDA do canal desconectado. O canal se cura com qualquer
        chamada bem-sucedida, mas com ele caído o envio e o reenvio ficam
        bloqueados — sem uma sonda que não seja envio, trocar o token não
        adiantaria: o sistema recusaria tudo para sempre esperando um sucesso
        que ele mesmo impedia de acontecer.

        Levanta `WhatsAppError` quando a Meta recusa. Não escolhi `send_text`
        de propósito: a sonda não pode mandar mensagem para ninguém.
        """
        try:
            # `phone_id` é keyword-only no PyWa (assinatura com `*`).
            numero = self._wa.get_business_phone_number(phone_id=self.channel.phone_number_id)
        except pywa_errors.WhatsAppError as exc:
            raise _translate(exc) from exc
        except httpx.TransportError as exc:
            raise _rede(exc) from exc
        return {
            "display_phone_number": getattr(numero, "display_phone_number", ""),
            "verified_name": getattr(numero, "verified_name", ""),
            "quality_rating": str(getattr(numero, "quality_rating", "") or ""),
        }

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
                page = self._wa.api.get_templates(waba_id=self.waba_id, pagination=pagination)
            except pywa_errors.WhatsAppError as exc:
                raise _translate(exc) from exc
            except httpx.TransportError as exc:
                raise _rede(exc) from exc
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
                {"limit": TEMPLATES_PAGE_SIZE, "after": after} if has_next and after else None
            )
        return templates

    # ----------------------------- webhook ------------------------------- #

    def parse_webhook(self, payload: dict) -> list[WhatsAppEvent]:
        return parse_meta_webhook(payload)
