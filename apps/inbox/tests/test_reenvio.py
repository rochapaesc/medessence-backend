"""
Reenvio do que não saiu (Bloco B).

A regra vem do Chatwoot (`MessageError.vue`): reenviar é ação HUMANA, com
guarda de 24h. Nenhum dos três repositórios de referência reenvia sozinho ao
reconectar — mensagem presa meia hora pode ter virado assunto vencido.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.inbox.choices import MessageKind, MessageStatus, SenderKind
from apps.inbox.models import Message

MESSAGES = "/api/v1/messages/"
COUNTERS = "/api/v1/conversations/counters/"


@pytest.fixture
def logado(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


def _falha(conversation, *, horas_atras=0, template="", erro="Canal desconectado"):
    return Message.objects.create(
        clinic=conversation.clinic,
        conversation=conversation,
        sender_kind=SenderKind.AGENT,
        kind=MessageKind.TEMPLATE if template else MessageKind.TEXT,
        template_name=template,
        body="Seu resultado está pronto.",
        status=MessageStatus.FAILED,
        status_error=erro,
        wa_timestamp=timezone.now() - timedelta(hours=horas_atras),
    )


def _abre_janela(conversation):
    conversation.last_inbound_at = timezone.now() - timedelta(minutes=5)
    conversation.save(update_fields=["last_inbound_at"])


def _derruba_canal(conversation):
    canal = conversation.channel
    canal.disconnected_at = timezone.now()
    canal.disconnect_reason = "Credencial expirada"
    canal.save(update_fields=["disconnected_at", "disconnect_reason"])


# ────────────────────────────── uma mensagem ──────────────────────────────


def test_reenvia_mensagem_que_falhou(logado, inbox_a):
    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    message = _falha(conversation)

    resposta = logado.post(f"{MESSAGES}{message.pk}/resend/")

    assert resposta.status_code == 200
    message.refresh_from_db()
    # Some o motivo antigo e sai do vermelho: manter o erro faria o balão
    # continuar falho enquanto a nova tentativa acontece. (Nos testes o envio
    # roda na hora, então aqui já chega em `sent`.)
    assert message.status != MessageStatus.FAILED
    assert message.status_error == ""


def test_nao_reenvia_fora_da_janela_de_24h(logado, inbox_a):
    """A guarda do Chatwoot. Passou da janela, reenviar produziria a MESMA
    falha — o botão só existiria para frustrar quem clica."""
    conversation = inbox_a["conversation"]
    conversation.last_inbound_at = timezone.now() - timedelta(hours=30)
    conversation.save(update_fields=["last_inbound_at"])
    message = _falha(conversation)

    resposta = logado.post(f"{MESSAGES}{message.pk}/resend/")

    assert resposta.status_code == 400
    assert "template" in str(resposta.data)
    message.refresh_from_db()
    assert message.status == MessageStatus.FAILED


def test_template_reenvia_mesmo_fora_da_janela(logado, inbox_a):
    """Template é justamente o que existe para fora da janela."""
    conversation = inbox_a["conversation"]
    conversation.last_inbound_at = timezone.now() - timedelta(hours=30)
    conversation.save(update_fields=["last_inbound_at"])
    message = _falha(conversation, template="confirmacao_consulta")

    resposta = logado.post(f"{MESSAGES}{message.pk}/resend/")

    assert resposta.status_code == 200


def test_nao_reenvia_com_o_canal_ainda_caido(logado, inbox_a):
    """Reenviar para canal morto só produz a mesma falha, agora com a recepção
    achando que resolveu."""
    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    _derruba_canal(conversation)
    message = _falha(conversation)

    resposta = logado.post(f"{MESSAGES}{message.pk}/resend/")

    assert resposta.status_code == 400
    assert "desconectado" in str(resposta.data)


def test_nao_reenvia_mensagem_que_nao_falhou(logado, inbox_a):
    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    message = _falha(conversation)
    message.status = MessageStatus.SENT
    message.save(update_fields=["status"])

    resposta = logado.post(f"{MESSAGES}{message.pk}/resend/")

    assert resposta.status_code == 400


# ──────────────────────────────── em lote ────────────────────────────────


def test_reenvio_em_lote_conta_o_que_saiu_e_o_que_ficou(logado, inbox_a, inbox_b):
    """
    A faixa não pode sumir calada: se 3 falharam e só 2 saem, a recepção tem
    de saber que 1 continua parada.
    """
    aberta = inbox_a["conversation"]
    _abre_janela(aberta)
    _falha(aberta)
    _falha(aberta)

    # Mesma clínica, conversa com a janela FECHADA: esta não sai.
    from apps.inbox.models import Conversation
    from apps.patients.models import Contact

    contato = Contact.objects.create(
        clinic=aberta.clinic, wa_id="5585911112222", display_name="Outro"
    )
    fechada = Conversation.objects.create(
        clinic=aberta.clinic,
        channel=aberta.channel,
        contact=contato,
        last_inbound_at=timezone.now() - timedelta(hours=40),
    )
    _falha(fechada)

    # Outra clínica: nunca entra na conta.
    _falha(inbox_b["conversation"])

    resposta = logado.post(f"{MESSAGES}resend-failed/")

    assert resposta.status_code == 200
    assert resposta.data == {"reenviadas": 2, "fora_da_janela": 1}


def test_lote_ignora_falha_de_mais_de_24h(logado, inbox_a):
    """Passou de 24h a mensagem sai da lista — nem é oferecida."""
    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    _falha(conversation, horas_atras=30)

    resposta = logado.post(f"{MESSAGES}resend-failed/")

    assert resposta.data == {"reenviadas": 0, "fora_da_janela": 0}


def test_nota_interna_nunca_entra_no_reenvio(logado, inbox_a):
    """Ela nunca tentou sair: não há o que reenviar."""
    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    nota = _falha(conversation)
    nota.is_internal = True
    nota.save(update_fields=["is_internal"])

    resposta = logado.post(f"{MESSAGES}resend-failed/")

    assert resposta.data["reenviadas"] == 0


# ──────────────────────────── contagem na faixa ───────────────────────────


def test_counters_informa_quantas_ficaram_presas(logado, inbox_a):
    """A faixa verde de 'reconectado' precisa do número para oferecer o
    conserto — descobrir só no próximo F5 é tarde."""
    conversation = inbox_a["conversation"]
    _falha(conversation)
    _falha(conversation)
    _falha(conversation, horas_atras=30)  # velha demais, não conta

    resposta = logado.get(COUNTERS)

    assert resposta.data["channel"]["failed_messages"] == 2


def test_falha_de_envio_avisa_a_tela_pelo_id(inbox_a, monkeypatch):
    """
    O balão ficava eterno em 'enviando' porque o evento era endereçado por
    wamid — que uma falha de envio nunca chega a ter. Agora vai pelo id.
    """
    from apps.inbox.services import send_message
    from apps.integrations.whatsapp.exceptions import WhatsAppError

    eventos = []
    monkeypatch.setattr("apps.inbox.realtime._broadcast", lambda cid, data: eventos.append(data))

    class _Quebrado:
        def send_text(self, *args, **kwargs):
            raise WhatsAppError("número inválido")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider", lambda c: _Quebrado()
    )

    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    message = Message.objects.create(
        clinic=conversation.clinic,
        conversation=conversation,
        sender_kind=SenderKind.AGENT,
        kind=MessageKind.TEXT,
        body="oi",
        wa_timestamp=timezone.now(),
    )

    send_message(message)

    status = [e for e in eventos if e["event"] == "message:status"]
    assert status, "a tela precisa saber que a mensagem não saiu"
    assert status[0]["message_id"] == message.pk
    assert status[0]["status"] == MessageStatus.FAILED
    assert status[0]["error"]


def test_reenvio_recarimba_o_horario_e_move_para_o_fim(logado, inbox_a):
    """
    O balão passa a valer AGORA, não na hora em que foi escrito.

    No celular do paciente a mensagem chega neste instante; mantê-la às 20:22
    de ontem abriria um buraco na conversa — uma fala antiga no meio do
    passado que o outro lado só leu hoje.
    """
    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    message = _falha(conversation, horas_atras=14)
    antes = message.wa_timestamp

    logado.post(f"{MESSAGES}{message.pk}/resend/")

    message.refresh_from_db()
    conversation.refresh_from_db()
    assert message.wa_timestamp > antes
    # A conversa sobe na fila com a prévia nova: a reenviada é a última do fio.
    assert conversation.last_message_at == message.wa_timestamp
    assert conversation.last_message_preview == message.body[:200]


def test_lote_reenvia_da_mais_antiga_para_a_mais_nova(logado, inbox_a):
    """Cada reenvio carimba o horário de agora — fora de ordem, o paciente
    receberia a resposta antes da pergunta."""
    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    primeira = _falha(conversation, horas_atras=5)
    primeira.body = "primeira"
    primeira.save(update_fields=["body"])
    segunda = _falha(conversation, horas_atras=2)
    segunda.body = "segunda"
    segunda.save(update_fields=["body"])

    logado.post(f"{MESSAGES}resend-failed/")

    primeira.refresh_from_db()
    segunda.refresh_from_db()
    assert primeira.wa_timestamp <= segunda.wa_timestamp


def test_conversa_RESOLVIDA_nao_entra_na_contagem_de_presas(logado, inbox_a):
    """
    Atendimento encerrado não pede reenvio.

    Quem resolveu decidiu que o assunto acabou; a faixa cobrando "3 mensagens
    não saíram" mandaria a recepção reabrir conversa fechada para mandar algo
    vencido. (Pedido do usuário em 29/07.)
    """
    from apps.inbox.choices import ConversationStatus

    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    _falha(conversation)
    conversation.status = ConversationStatus.RESOLVED
    conversation.save(update_fields=["status"])

    resposta = logado.get(COUNTERS)

    assert resposta.data["channel"]["failed_messages"] == 0
