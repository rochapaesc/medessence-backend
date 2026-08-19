"""
API da conexão do canal (§4.3.3, F2.7).

  GET  /channel/                 : o estado do canal, para a tela desenhar.
  GET  /channel/signup-config/   : o que o SDK da Meta precisa no navegador.
  POST /channel/connect/         : o fluxo inteiro (RF-CON-2).
  POST /channel/disconnect/      : para de usar o número, sem apagar nada.

⚠️ A configuração vem por endpoint (RF-CON-1.3) e NUNCA inclui o `app_secret`:
o navegador recebe `app_id` e `config_id`, que são públicos por natureza (vão
na URL do popup da Meta), e nada além disso.
"""

import logging

from django.conf import settings
from rest_framework import status
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api.permissions import IsClinicManager, IsClinicMember
from apps.core.context import resolve_active_membership
from apps.inbox.models import Channel
from apps.inbox.signup import NumeroJaConectado, conectar_canal, desconectar_canal
from apps.integrations.whatsapp.exceptions import (
    WhatsAppError,
    WhatsAppNotConfiguredError,
)
from apps.integrations.whatsapp.meta import signup as meta_signup

logger = logging.getLogger(__name__)


def estado_do_canal(clinic) -> dict:
    """
    O que a tela mostra sobre o canal.

    ⚠️ Identificador da Meta NÃO sai daqui (RF-CON-1.1): o gestor não tem o que
    fazer com um `waba_id`, e mostrar credencial na tela é convite para colá-la
    num suporte de terceiro. O que ele precisa é o número, o nome e se está no ar.
    """
    canal = Channel.objects.filter(clinic=clinic, is_test=False).first()
    if canal is None:
        return {"conectado": False, "canal": None}

    return {
        "conectado": not canal.disconnected,
        "canal": {
            "uuid": str(canal.uuid),
            "display_number": canal.display_number,
            "verified_name": canal.verified_name,
            "provider": canal.provider,
            "connection_source": canal.connection_source,
            "is_coexistence": canal.is_coexistence,
            "connected_at": canal.connected_at,
            "disconnected": canal.disconnected,
            "disconnect_reason": canal.disconnect_reason,
        },
    }


class ChannelView(APIView):
    """
    O estado do canal. LER é de todo mundo da clínica: o atendente precisa
    saber que o WhatsApp caiu, e é ele quem está com o paciente na linha.
    Quem CONECTA é só o gestor, nas views abaixo.
    """

    permission_classes = [IsClinicMember]

    def get(self, request):
        clinic = resolve_active_membership(request).clinic
        return Response(estado_do_canal(clinic))


class ChannelSignupConfigView(APIView):
    """
    O que o SDK da Meta precisa para abrir o popup (RF-CON-1.3).

    Serve pelo servidor em vez de ficar cravado no front porque o app publicado
    é público e reconstruí-lo para trocar um id é caro. `disponivel` falso faz a
    tela explicar que a plataforma ainda não está configurada, em vez de abrir
    um popup que a Meta recusa com uma mensagem que o gestor não entende.
    """

    permission_classes = [IsClinicManager]

    def get(self, request):
        return Response(
            {
                "disponivel": meta_signup.app_configurado(),
                "app_id": settings.WHATSAPP_APP_ID,
                "config_id": settings.WHATSAPP_CONFIG_ID,
                "graph_version": settings.WHATSAPP_GRAPH_VERSION,
            }
        )


class ChannelConnectView(APIView):
    """
    Recebe o que o popup devolveu e executa o fluxo (RF-CON-2).

    O `business_id` chega junto no evento da Meta e é aceito por fidelidade ao
    fluxo de referência, mas não é usado: o que identifica a conta para tudo o
    que fazemos é o `waba_id`.
    """

    permission_classes = [IsClinicManager]

    def post(self, request):
        clinic = resolve_active_membership(request).clinic
        code = (request.data.get("code") or "").strip()
        waba_id = (request.data.get("waba_id") or "").strip()
        phone_number_id = (request.data.get("phone_number_id") or "").strip()

        faltando = [n for n, v in (("code", code), ("waba_id", waba_id)) if not v]
        if faltando:
            return Response(
                {
                    "detail": "A Meta não devolveu tudo o que era preciso. Tente conectar de novo.",
                    "code": "signup_incompleto",
                    "faltando": faltando,
                },
                status=status.HTTP_400_BAD_REQUEST,
            )

        try:
            canal = conectar_canal(
                clinic,
                request.user,
                code=code,
                waba_id=waba_id,
                phone_number_id=phone_number_id,
                request=request,
            )
        except NumeroJaConectado as exc:
            # 409: não é erro de quem chamou nem da Meta, é conflito de estado.
            return Response(
                {"detail": str(exc), "code": "numero_em_outra_clinica"},
                status=status.HTTP_409_CONFLICT,
            )
        except WhatsAppNotConfiguredError as exc:
            logger.error("Cadastro incorporado indisponível: %s", exc)
            return Response(
                {
                    "detail": (
                        "A conexão com o WhatsApp ainda não está disponível. "
                        "Fale com o suporte do MedEssence."
                    ),
                    "code": "app_nao_configurado",
                },
                status=status.HTTP_503_SERVICE_UNAVAILABLE,
            )
        except WhatsAppError as exc:
            # A frase é a da Meta, já traduzida para o usuário final quando ela
            # manda as duas versões. Estourar 500 aqui esconderia o motivo real.
            return Response(
                {"detail": str(exc), "code": "meta_recusou"},
                status=status.HTTP_400_BAD_REQUEST,
            )

        return Response(estado_do_canal(canal.clinic), status=status.HTTP_201_CREATED)


class ChannelDisconnectView(APIView):
    """Para de usar o número. Não apaga canal, conversa nem mensagem."""

    permission_classes = [IsClinicManager]

    def post(self, request):
        clinic = resolve_active_membership(request).clinic
        canal = desconectar_canal(
            clinic,
            request.user,
            motivo="Desconectado pela clínica.",
            request=request,
        )
        if canal is None:
            return Response(
                {"detail": "Esta clínica não tem WhatsApp conectado.", "code": "sem_canal"},
                status=status.HTTP_404_NOT_FOUND,
            )
        return Response(estado_do_canal(clinic))
