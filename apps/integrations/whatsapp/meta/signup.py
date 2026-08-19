"""
As chamadas do cadastro incorporado da Meta (§4.3.3, RF-CON-2).

Por que NÃO usa o `MetaAdapter`: ele nasce de um canal que já tem token e
`phone_number_id` (levanta `WhatsAppNotConfiguredError` sem eles), e aqui é
justamente o contrário. Estas três chamadas acontecem ANTES de existir canal,
e duas delas nem sequer falam de um número: falam do app da plataforma e da
conta do cliente.

⚠️ O `app_secret` só existe deste lado. O fluxo de referência da Meta troca o
código por token dentro do navegador, com o segredo à vista no código-fonte da
página; quem abrisse o fonte teria o app inteiro (RF-CON-1.2).
"""

import logging

import httpx
from django.conf import settings

from apps.integrations.whatsapp.exceptions import (
    WhatsAppAuthError,
    WhatsAppError,
    WhatsAppNotConfiguredError,
    WhatsAppUnavailableError,
)

logger = logging.getLogger(__name__)

# A Meta responde devagar quando a conta é grande; 30s é o mesmo teto que o
# resto da integração usa para chamadas de leitura.
TIMEOUT = 30.0


def graph_url(caminho: str) -> str:
    versao = settings.WHATSAPP_GRAPH_VERSION
    return f"https://graph.facebook.com/{versao}/{caminho.lstrip('/')}"


def app_configurado() -> bool:
    """
    O app da plataforma tem o que o cadastro incorporado exige?

    Sem isto a tela ofereceria um botão que abre um popup vazio, e o gestor
    veria a Meta reclamar de um app que ele não configurou nem conhece.
    """
    return bool(
        settings.WHATSAPP_APP_ID
        and settings.WHATSAPP_APP_SECRET
        and settings.WHATSAPP_CONFIG_ID
    )


def _erro_da_meta(resposta: httpx.Response) -> WhatsAppError:
    """
    O erro da Meta em algo que dá para mostrar e para depurar.

    Ela devolve `error.error_user_title`/`error_user_msg` quando a mensagem é
    para o usuário final, e `error.message` quando é para quem integra. O
    primeiro vence; o segundo vai para o log de qualquer jeito, porque é ele
    que traz o código que se procura na documentação.
    """
    try:
        erro = (resposta.json() or {}).get("error", {}) or {}
    except ValueError:
        erro = {}

    tecnico = erro.get("message", "") or resposta.text[:300]
    logger.warning(
        "Cadastro incorporado: a Meta recusou (HTTP %s, código %s): %s",
        resposta.status_code,
        erro.get("code", "?"),
        tecnico,
    )

    legivel = (erro.get("error_user_title") or "").strip()
    detalhe = (erro.get("error_user_msg") or "").strip()
    if legivel and detalhe:
        legivel = f"{legivel.rstrip('.')}. {detalhe}"
    mensagem = legivel or tecnico or "A Meta recusou a chamada."

    if resposta.status_code in (401, 403) or erro.get("code") in (190, 102):
        return WhatsAppAuthError(mensagem)
    if resposta.status_code >= 500:
        return WhatsAppUnavailableError(mensagem)
    return WhatsAppError(mensagem)


def _pedir(metodo: str, url: str, **kwargs) -> dict:
    try:
        resposta = httpx.request(metodo, url, timeout=TIMEOUT, **kwargs)
    except httpx.TransportError as exc:
        # Rede caindo é transitório: quem chamou mostra "tente de novo", em vez
        # de dizer que a Meta recusou, que seria diagnóstico errado.
        raise WhatsAppUnavailableError(f"Falha de rede com a Meta: {exc}") from exc

    if resposta.status_code >= 400:
        raise _erro_da_meta(resposta)

    try:
        return resposta.json() or {}
    except ValueError as exc:
        raise WhatsAppError("A Meta respondeu em um formato inesperado.") from exc


def trocar_codigo_por_token(code: str) -> str:
    """
    O `code` do popup vira o token do CLIENTE (RF-CON-2.1).

    ⚠️ Token vazio na resposta é erro, e não canal salvo pela metade: sem ele
    nada mais do fluxo funciona, e um canal gravado sem credencial ficaria
    parecendo conectado enquanto recusa toda mensagem.
    """
    if not code:
        raise WhatsAppError("A Meta não devolveu o código de autorização.")
    if not app_configurado():
        raise WhatsAppNotConfiguredError(
            "O aplicativo da plataforma na Meta não está configurado "
            "(WHATSAPP_APP_ID, WHATSAPP_APP_SECRET e WHATSAPP_CONFIG_ID)."
        )

    dados = _pedir(
        "POST",
        graph_url("oauth/access_token"),
        params={
            "client_id": settings.WHATSAPP_APP_ID,
            "client_secret": settings.WHATSAPP_APP_SECRET,
            "code": code,
        },
    )
    token = (dados.get("access_token") or "").strip()
    if not token:
        raise WhatsAppError("A Meta não devolveu o token de acesso.")
    return token


def numero_da_conta(waba_id: str, token: str, phone_number_id: str = "") -> dict:
    """
    O número do WABA recém-conectado (RF-CON-2.2).

    Pede os campos que a TELA precisa, e não só o id: o número exibido e o nome
    verificado são o que a clínica reconhece como sendo dela. Sem eles a tela
    diria "conectado" sem dizer conectado a quê.

    Quando o popup já informou qual número é (`phone_number_id`), ele vence a
    lista, como no `phone_info_service.rb` do Chatwoot. Sem isso, a conta com
    mais de um número poderia ligar o errado.
    """
    dados = _pedir(
        "GET",
        graph_url(f"{waba_id}/phone_numbers"),
        params={
            "fields": "id,display_phone_number,verified_name,code_verification_status,platform_type",
            "access_token": token,
        },
    )
    numeros = dados.get("data") or []
    if not numeros:
        raise WhatsAppError(
            "A conta do WhatsApp conectada não tem nenhum número. "
            "Conclua o cadastro do número na Meta e tente de novo."
        )

    escolhido = None
    if phone_number_id:
        escolhido = next((n for n in numeros if n.get("id") == phone_number_id), None)
    return escolhido or numeros[0]


def assinar_webhook(waba_id: str, token: str) -> None:
    """
    Inscreve o app da plataforma no WABA do cliente (RF-CON-2.3).

    ⚠️ Sem este passo o canal fica salvo, bonito e MUDO: nenhum webhook chega e
    nada acusa o problema, porque do nosso lado está tudo gravado.

    ⚠️ Isto inscreve o APP no WABA. **Quais campos** chegam (`messages`,
    `smb_message_echoes`, `smb_app_state_sync`...) é configuração do painel do
    app na Meta, não desta chamada (P19). Assinar só `messages` lá faz a
    coexistência falhar em silêncio.
    """
    _pedir(
        "POST",
        graph_url(f"{waba_id}/subscribed_apps"),
        headers={"Authorization": f"Bearer {token}"},
    )
