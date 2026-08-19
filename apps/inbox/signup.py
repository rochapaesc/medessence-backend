"""
Conectar o WhatsApp de uma clínica pelo cadastro incorporado (§4.3.3).

Este módulo é o dono da SEQUÊNCIA (RF-CON-2): trocar o código por token,
descobrir o número, assinar o webhook e gravar o canal. Ele fica fora do
viewset porque a mesma sequência serve à tela, ao comando de manutenção e a
qualquer porta futura, e porque a ordem dos passos é regra de negócio: assinar
o webhook depois de gravar deixaria uma janela em que a clínica parece
conectada e não recebe nada.

A ordem é a mesma do fluxo de referência da Meta, com uma diferença que é o
ponto todo: o `app_secret` nunca sai daqui (RF-CON-1.2).
"""

import logging

from django.db import transaction
from django.utils import timezone

from apps.core.audit import log_action
from apps.inbox.choices import ChannelSource, WhatsAppProviderKind
from apps.inbox.models import Channel
from apps.inbox.realtime import notify_channel_health
from apps.integrations.whatsapp.exceptions import WhatsAppError
from apps.integrations.whatsapp.meta import signup as meta_signup

logger = logging.getLogger(__name__)


class NumeroJaConectado(WhatsAppError):
    """
    O número já pertence ao canal de OUTRA clínica (RF-CON-2.6).

    Não é erro da Meta e não adianta tentar de novo: alguém precisa desconectar
    o número da outra clínica antes. Classe própria porque a tela diz uma frase
    diferente, com o que fazer.
    """


def conectar_canal(clinic, user, *, code, waba_id, phone_number_id="", request=None):
    """
    Roda o fluxo inteiro e devolve o `Channel` conectado (RF-CON-2).

    ⚠️ As chamadas à Meta acontecem ANTES da transação, e de propósito: elas
    demoram e podem falhar, e segurar transação aberta durante rede de terceiro
    prende conexão do banco. Nada é gravado até a Meta ter dito sim a tudo.
    """
    token = meta_signup.trocar_codigo_por_token(code)
    numero = meta_signup.numero_da_conta(waba_id, token, phone_number_id=phone_number_id)
    numero_id = numero.get("id", "")
    if not numero_id:
        raise WhatsAppError("A Meta não informou qual número foi conectado.")

    _recusar_numero_de_outra_clinica(clinic, numero_id)

    # Assina ANTES de gravar: se falhar, a clínica continua sem canal em vez de
    # ficar com um canal que nunca receberia mensagem nenhuma.
    meta_signup.assinar_webhook(waba_id, token)

    with transaction.atomic():
        canal, criado = _gravar_canal(clinic, waba_id, numero, token)

    # A faixa de canal fora do ar precisa sumir sozinha para quem está com o
    # Inbox aberto: o mesmo evento que a acende é o que a apaga.
    notify_channel_health(canal)

    log_action(
        user=user,
        action="CREATE" if criado else "UPDATE",
        resource="Channel",
        resource_id=canal.pk,
        # Sem token, sem `code`: o que importa auditar é QUEM ligou QUAL número.
        payload={
            "after": {
                "waba_id": waba_id,
                "phone_number_id": numero_id,
                "display_number": canal.display_number,
                "connection_source": canal.connection_source,
            }
        },
        request=request,
        clinic=clinic,
    )
    logger.info(
        "Canal %s para a clínica %s pelo cadastro incorporado (número %s)",
        "criado" if criado else "reconectado",
        clinic.pk,
        numero_id,
    )
    return canal


def _recusar_numero_de_outra_clinica(clinic, phone_number_id: str) -> None:
    """
    RF-CON-2.6, conferido ANTES de falar com a Meta de novo.

    A constraint do banco é a garantia; esta checagem existe para o erro chegar
    à tela como frase, e não como violação de integridade traduzida em 500.
    """
    dono = (
        Channel.objects.filter(phone_number_id=phone_number_id)
        .exclude(clinic=clinic)
        .first()
    )
    if dono is not None:
        raise NumeroJaConectado(
            "Este número já está conectado em outra clínica. "
            "Desconecte-o lá antes de ligar aqui."
        )


def _gravar_canal(clinic, waba_id: str, numero: dict, token: str):
    """
    Cria ou RECONECTA (RF-CON-2.5).

    Reconectar reaproveita o registro existente porque as conversas, mensagens
    e execuções de fluxo apontam para ele: canal novo levaria o histórico da
    clínica junto, e ainda esbarraria na trava de um canal por clínica.
    """
    canal = Channel.objects.filter(clinic=clinic, is_test=False).first()
    criado = canal is None
    if criado:
        canal = Channel(clinic=clinic)

    canal.provider = WhatsAppProviderKind.META
    canal.waba_id = waba_id
    canal.phone_number_id = numero.get("id", "")
    canal.display_number = numero.get("display_phone_number", "") or canal.display_number
    canal.verified_name = numero.get("verified_name", "") or canal.verified_name
    canal.connection_source = ChannelSource.EMBEDDED_SIGNUP
    canal.is_coexistence = True
    canal.connected_at = timezone.now()
    # ⚠️ Preserva o resto de `credentials` (o `app_secret` por canal do WABA
    # compartilhado mora ali, commit 2fea4dd): sobrescrever o dicionário
    # inteiro tiraria do ar a conferência de assinatura do webhook.
    canal.credentials = {**(canal.credentials or {}), "access_token": token}

    # Reconectar CURA o canal: quem acabou de autorizar de novo não pode
    # continuar vendo a faixa de "desconectado" na tela.
    canal.auth_error_count = 0
    canal.disconnected_at = None
    canal.disconnect_reason = ""
    canal.save()
    return canal, criado


def desconectar_canal(clinic, user, *, motivo: str = "", request=None) -> Channel | None:
    """
    Marca o canal como fora do ar, sem apagá-lo (RF-CON-5.4 e o botão da tela).

    ⚠️ NÃO apaga o registro nem as credenciais. A clínica que desliga hoje e
    religa amanhã tem de reencontrar as conversas dela, e apagar o canal
    derrubaria conversa, mensagem e execução de fluxo por cascata. O que
    desconectar faz é parar de enviar e dizer por quê.
    """
    canal = Channel.objects.filter(clinic=clinic, is_test=False).first()
    if canal is None:
        return None

    canal.disconnected_at = canal.disconnected_at or timezone.now()
    canal.disconnect_reason = (motivo or "Desconectado pela clínica.")[:200]
    canal.save(update_fields=["disconnected_at", "disconnect_reason", "updated_at"])
    notify_channel_health(canal)

    log_action(
        user=user,
        action="UPDATE",
        resource="Channel",
        resource_id=canal.pk,
        payload={"after": {"disconnect_reason": canal.disconnect_reason}},
        request=request,
        clinic=clinic,
    )
    return canal
