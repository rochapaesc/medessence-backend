"""Tempo real (§12): emissão de eventos por clínica, contrato e isolamento;
autenticação do WebSocket (JWT + membership).

Os testes exercitam o channel layer diretamente (InMemoryChannelLayer) via
async_to_sync - sem WebSocket real, mas cobrindo notify_* → group_send, os
nomes de grupo por clínica e o gate de autenticação do middleware.
"""

from asgiref.sync import async_to_sync
from channels.layers import get_channel_layer

from apps.inbox.choices import SenderKind
from apps.inbox.services import ingest_events
from apps.inbox.tests.conftest import make_message
from apps.integrations.whatsapp.events import parse_meta_webhook
from apps.integrations.whatsapp.fake.adapter import build_inbound_payload


def _subscribe(clinic_id):
    """Cria um canal e o adiciona ao grupo da clínica; devolve (layer, canal)."""
    layer = get_channel_layer()
    channel_name = async_to_sync(layer.new_channel)()
    async_to_sync(layer.group_add)(f"inbox_clinic_{clinic_id}", channel_name)
    return layer, channel_name


def _receive(layer, channel_name):
    return async_to_sync(layer.receive)(channel_name)


def test_inbound_emite_message_new_e_conversation_updated(clinic_a, inbox_a):
    layer, channel_name = _subscribe(clinic_a.id)
    payload = build_inbound_payload(wa_id="5585900000010", body="chegou")
    ingest_events(inbox_a["channel"], parse_meta_webhook(payload))

    first = _receive(layer, channel_name)["data"]
    second = _receive(layer, channel_name)["data"]
    events = {first["event"], second["event"]}
    assert events == {"message:new", "conversation:updated"}


def test_status_emite_message_status(clinic_a, inbox_a):
    make_message(inbox_a["conversation"], sender_kind=SenderKind.AGENT, mid="wamid.s1")
    layer, channel_name = _subscribe(clinic_a.id)
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "statuses": [
                                {"id": "wamid.s1", "status": "read", "timestamp": "1710000000"}
                            ]
                        }
                    }
                ]
            }
        ]
    }
    ingest_events(inbox_a["channel"], parse_meta_webhook(payload))
    data = _receive(layer, channel_name)["data"]
    assert data["event"] == "message:status"
    assert data["provider_message_id"] == "wamid.s1"
    assert data["status"] == "read"


def test_evento_nao_vaza_entre_clinicas(clinic_a, clinic_b, inbox_a, inbox_b):
    # Assina o grupo da clínica B; a ingestão acontece na clínica A.
    layer, channel_b = _subscribe(clinic_b.id)
    ingest_events(
        inbox_a["channel"],
        parse_meta_webhook(build_inbound_payload(wa_id="5585900000020", body="a")),
    )
    # Nada deve chegar ao grupo da B.
    assert async_to_sync(_nothing_within)(layer, channel_b) is True


async def _nothing_within(layer, channel_name):
    import asyncio

    try:
        await asyncio.wait_for(layer.receive(channel_name), timeout=0.2)
        return False
    except TimeoutError:
        return True


# --------------------------------------------------------------------- #
# Autenticação do WebSocket (middleware JWT + membership)
# --------------------------------------------------------------------- #


def _resolve(token, clinic_id):
    from apps.inbox.ws_auth import JWTClinicMiddleware

    middleware = JWTClinicMiddleware(app=None)
    return async_to_sync(middleware._resolve)(token, clinic_id)


def test_ws_auth_token_valido_resolve_membership(clinic_a, attendant_a):
    from rest_framework_simplejwt.tokens import AccessToken

    token = str(AccessToken.for_user(attendant_a))
    membership = _resolve(token, clinic_a.id)
    assert membership is not None
    assert membership.clinic_id == clinic_a.id
    assert membership.user_id == attendant_a.id


def test_ws_auth_token_invalido_recusa(clinic_a, attendant_a):
    assert _resolve("token-invalido", clinic_a.id) is None


def test_ws_auth_sem_vinculo_na_clinica_recusa(clinic_b, attendant_a):
    from rest_framework_simplejwt.tokens import AccessToken

    token = str(AccessToken.for_user(attendant_a))  # atendente é da clinic_a
    assert _resolve(token, clinic_b.id) is None


def test_ws_auth_sem_token_recusa(clinic_a):
    assert _resolve(None, clinic_a.id) is None


# NOTA: o handshake completo do consumer (connect→accept, group_send→send,
# close 4401 sem membership) foi validado manualmente via
# asgiref.testing.ApplicationCommunicator. Não fica na suíte automatizada
# porque o async_to_sync conflita com o teardown de conexões do pytest-django
# (exigiria pytest-asyncio + gestão async de DB). A lógica de grupo/contrato e
# o gate de auth já são cobertos pelos testes acima.
