"""Sanitização de HTML vindo de terceiros (§6.2, §15) - whitelist via nh3."""

import nh3

ALLOWED_TAGS = {
    "p",
    "br",
    "b",
    "strong",
    "i",
    "em",
    "u",
    "s",
    "ul",
    "ol",
    "li",
    "span",
    "a",
    "h1",
    "h2",
    "h3",
    "blockquote",
}
ALLOWED_ATTRIBUTES = {"a": {"href"}, "span": {"style"}}


def sanitize_html(value: str | None) -> str:
    """
    Limpa HTML externo antes de armazenar/exibir. Remove scripts, handlers
    de evento e tags fora da whitelist - nunca confiar em HTML do EHR.
    """
    if not value:
        return ""
    return nh3.clean(
        value,
        tags=ALLOWED_TAGS,
        attributes=ALLOWED_ATTRIBUTES,
        link_rel="noopener noreferrer",
    )
