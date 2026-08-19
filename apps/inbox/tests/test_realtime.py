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


def _drain(layer, channel_name, limit=6):
    """Recebe o que houver SEM bloquear: `layer.receive` espera para sempre,
    e um teste que espera para sempre não falha - trava."""
    import asyncio

    async def um():
        try:
            return await asyncio.wait_for(layer.receive(channel_name), timeout=0.3)
        except asyncio.TimeoutError:
            return None

    eventos = []
    for _ in range(limit):
        recebido = async_to_sync(um)()
        if recebido is None:
            break
        eventos.append(recebido["data"])
    return eventos


def test_numero_novo_emite_conversation_new_com_a_linha_inteira(clinic_a, inbox_a):
    """
    ⚠️ Quem escreve pela PRIMEIRA vez precisa do evento de conversa NOVA.

    Defeito real de 18/08/2026: só existia `conversation:updated`, e o cliente
    só sabe mexer em conversa que já está na lista dele. O aviso de uma que ele
    nunca viu era descartado em silêncio, e ela aparecia só depois de o
    atendente recarregar a página.

    A linha inteira viaja no evento de propósito: com só o id, uma campanha
    para mil pessoas viraria mil requisições em poucos minutos.
    """
    layer, channel_name = _subscribe(clinic_a.id)
    payload = build_inbound_payload(wa_id="5585900000010", body="chegou")
    ingest_events(inbox_a["channel"], parse_meta_webhook(payload))

    first = _receive(layer, channel_name)["data"]
    second = _receive(layer, channel_name)["data"]
    por_evento = {e["event"]: e for e in (first, second)}

    assert set(por_evento) == {"message:new", "conversation:new"}
    linha = por_evento["conversation:new"]["conversation"]
    # O que a lista precisa para desenhar a linha sem pedir nada ao servidor.
    for campo in ("id", "contact", "status", "attended_by", "last_message_at"):
        assert campo in linha, f"a linha da conversa nova precisa de {campo}"


def test_numero_conhecido_continua_emitindo_conversation_updated(clinic_a, inbox_a):
    """A conversa que já existe segue no caminho de sempre: quem a tem na lista
    só precisa dos campos que mudaram."""
    layer, channel_name = _subscribe(clinic_a.id)
    payload = build_inbound_payload(
        wa_id=inbox_a["contact"].wa_id, body="de novo eu"
    )
    ingest_events(inbox_a["channel"], parse_meta_webhook(payload))

    eventos = {
        _receive(layer, channel_name)["data"]["event"],
        _receive(layer, channel_name)["data"]["event"],
    }
    assert eventos == {"message:new", "conversation:updated"}


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
    # O cliente precisa saber QUAL thread atualizar.
    assert data["conversation_id"] == inbox_a["conversation"].pk


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


def test_consumer_diz_hello_e_responde_ping(manager_single_clinic, clinic_a):
    """
    O "hello" é o sinal de vida: o handshake do WebSocket no navegador é
    preguiçoso, e sem um primeiro frame o cliente marcava online no otimismo -
    era isso que fazia a tela piscar online/offline. O pong é o watchdog:
    queda silenciosa não fecha socket, então o cliente pinga e espera resposta.

    `ApplicationCommunicator` do asgiref, e não `channels.testing`: o __init__
    de channels.testing importa o daphne, que não instalamos (ASGI = uvicorn).
    """
    import json

    from asgiref.testing import ApplicationCommunicator

    from apps.accounts.models import Membership
    from apps.inbox.consumers import InboxConsumer

    membership = Membership.objects.get(user=manager_single_clinic, clinic=clinic_a)

    async def cenario():
        comm = ApplicationCommunicator(
            InboxConsumer.as_asgi(),
            {"type": "websocket", "path": "/ws/inbox/", "headers": [], "membership": membership},
        )
        await comm.send_input({"type": "websocket.connect"})
        aceito = await comm.receive_output(1)
        assert aceito["type"] == "websocket.accept"

        hello = json.loads((await comm.receive_output(1))["text"])
        assert hello == {"event": "hello"}

        await comm.send_input({"type": "websocket.receive", "text": '{"event": "ping"}'})
        pong = json.loads((await comm.receive_output(1))["text"])
        assert pong == {"event": "pong"}

        await comm.send_input({"type": "websocket.disconnect", "code": 1000})
        await comm.wait(1)

    async_to_sync(cenario)()


def test_envio_do_atendente_emite_conversation_updated(
    api_client, manager_single_clinic, inbox_a, django_capture_on_commit_callbacks
):
    """Antes, só o INBOUND emitia conversation:updated: a mensagem enviada não
    subia a conversa na fila de ninguém — achado do usuário em 28/07."""
    api_client.force_authenticate(manager_single_clinic)
    conversation = inbox_a["conversation"]
    # Inbound antes: sem ele a janela de 24h está fechada e o texto livre
    # leva 400 - o teste quer o caminho do envio, não o da janela.
    make_message(conversation, sender_kind=SenderKind.CONTACT)
    layer, channel_name = _subscribe(conversation.clinic_id)

    with django_capture_on_commit_callbacks(execute=True):
        resposta = api_client.post(
            "/api/v1/messages/",
            {"conversation": conversation.pk, "body": "subiu?"},
            format="json",
        )
    assert resposta.status_code == 201

    eventos = _drain(layer, channel_name)
    atualizacoes = [e for e in eventos if e["event"] == "conversation:updated"]
    assert atualizacoes, f"nenhum conversation:updated em {[e['event'] for e in eventos]}"
    assert atualizacoes[-1]["preview"] == "subiu?"
    # A data REAL vai no evento: sem ela o cliente ordenava pelo relógio local.
    assert atualizacoes[-1]["last_message_at"]
    # A posse viaja COMPLETA: escrever assume (RF-ATD-14), e o cliente decide
    # "sou eu?" pelo id — só o nome fazia quem enviou ver a própria tela travar.
    assert atualizacoes[-1]["attended_by"] == "agent"
    assert atualizacoes[-1]["assigned_to"] == manager_single_clinic.pk
