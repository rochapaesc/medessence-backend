"""
Saúde do canal (item 2 do fechamento do Inbox).

Desenho copiado do `Reauthorizable` do Chatwoot: erro de credencial CONTA, e o
canal só é dado como morto ao bater o limiar. Uma falha isolada é blip da Meta
— gritar lobo na primeira treina a equipe a ignorar o aviso.
"""

import pytest

from apps.inbox.choices import MessageKind, MessageStatus, SenderKind
from apps.inbox.models import Channel, Message
from apps.inbox.services import MOTIVO_CREDENCIAL, registrar_saude_do_canal, send_message
from apps.integrations.whatsapp.exceptions import (
    WhatsAppAuthError,
    WhatsAppUnavailableError,
)


@pytest.fixture
def logado(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


def _falha(channel, vezes, erro=None):
    for _ in range(vezes):
        registrar_saude_do_canal(channel, erro=erro or WhatsAppAuthError("190"))


def test_uma_falha_NAO_derruba_o_canal(inbox_a):
    """Blip da Meta não pode parar a clínica na tela."""
    canal = inbox_a["channel"]

    _falha(canal, 1)

    canal.refresh_from_db()
    assert canal.auth_error_count == 1
    assert not canal.disconnected, "uma falha isolada ainda não é credencial morta"


def test_a_SEGUNDA_falha_derruba(inbox_a):
    canal = inbox_a["channel"]

    _falha(canal, 2)

    canal.refresh_from_db()
    assert canal.disconnected
    assert canal.disconnect_reason == MOTIVO_CREDENCIAL


def test_falha_de_REDE_nao_conta_como_credencial(inbox_a):
    """5xx e rede caindo derrubariam o canal por instabilidade — e o aviso
    diria a coisa errada para o gestor."""
    canal = inbox_a["channel"]

    _falha(canal, 5, erro=WhatsAppUnavailableError("502"))

    canal.refresh_from_db()
    assert canal.auth_error_count == 0
    assert not canal.disconnected


def test_sucesso_CURA_o_canal_sozinho(inbox_a):
    """O `reauthorized!` do Chatwoot: ninguém precisa clicar em 'já arrumei'."""
    canal = inbox_a["channel"]
    _falha(canal, 2)
    canal.refresh_from_db()
    assert canal.disconnected

    registrar_saude_do_canal(canal)

    canal.refresh_from_db()
    assert not canal.disconnected
    assert canal.auth_error_count == 0
    assert canal.disconnect_reason == ""


def test_avisa_a_tela_UMA_vez_por_transicao(inbox_a, monkeypatch):
    """Sem isso, cada mensagem que tentasse sair depois emitiria o mesmo aviso
    de novo e a faixa piscaria."""
    avisos = []
    monkeypatch.setattr(
        "apps.inbox.realtime.notify_channel_health", lambda canal: avisos.append(canal.pk)
    )
    canal = inbox_a["channel"]

    _falha(canal, 4)  # cai na 2ª; as outras duas são só contagem

    assert len(avisos) == 1


def test_envio_com_credencial_morta_da_frase_HUMANA(inbox_a, monkeypatch, clinic_a):
    """O erro cru da Meta (`ExpiredAccessToken(code=190, fbtrace_id=…)`) chegava
    ao balão do atendente — texto de log, não de tela."""
    from django.utils import timezone

    from apps.integrations.whatsapp.fake.adapter import FakeWhatsAppAdapter

    monkeypatch.setattr(
        FakeWhatsAppAdapter,
        "send_text",
        lambda self, to, body, reply_to=None: (_ for _ in ()).throw(
            WhatsAppAuthError("ExpiredAccessToken(code=190, fbtrace_id=AXw...)")
        ),
    )
    mensagem = Message.objects.create(
        clinic=clinic_a,
        conversation=inbox_a["conversation"],
        sender_kind=SenderKind.AGENT,
        kind=MessageKind.TEXT,
        body="bom dia",
        wa_timestamp=timezone.now(),
    )

    send_message(mensagem)

    mensagem.refresh_from_db()
    assert mensagem.status == MessageStatus.FAILED
    assert mensagem.status_error == MOTIVO_CREDENCIAL
    assert "fbtrace" not in mensagem.status_error


def test_contadores_levam_a_saude_do_canal(inbox_a, logado):
    """A faixa precisa saber já na PRIMEIRA carga: quem abre o Inbox com o
    canal morto ficaria sem aviso, porque o evento só cobre a mudança."""
    canal = inbox_a["channel"]
    _falha(canal, 2)

    dados = logado.get("/api/v1/conversations/counters/").data

    assert dados["channel"]["disconnected"] is True
    assert dados["channel"]["reason"] == MOTIVO_CREDENCIAL


def test_canal_vivo_nao_alarma_a_tela(inbox_a, logado):
    dados = logado.get("/api/v1/conversations/counters/").data

    assert dados["channel"]["disconnected"] is False
    assert dados["channel"]["configured"] is True


def test_canal_fora_do_ar_vira_notificacao_do_gestor(inbox_a, clinic_a):
    """Chatwoot manda e-mail para os donos; aqui entra no sino que já existe."""
    from apps.notifications.services import build_feed

    _falha(inbox_a["channel"], 2)

    feed = build_feed(clinic_a)

    avisos = [item for item in feed.items if item.kind == "channel_down"]
    assert len(avisos) == 1
    assert avisos[0].severity == "danger"
    assert avisos[0].title == "WhatsApp desconectado"


def test_canal_vivo_nao_gera_notificacao(inbox_a, clinic_a):
    from apps.notifications.services import build_feed

    feed = build_feed(clinic_a)

    assert not [item for item in feed.items if item.kind == "channel_down"]


def test_verificacao_do_canal(inbox_a):
    """A propriedade é o que a tela lê — não o campo cru."""
    canal = Channel.objects.get(pk=inbox_a["channel"].pk)

    assert canal.disconnected is False


def test_counters_entrega_a_saude_do_processamento(api_client, manager_single_clinic, inbox_a):
    """
    A tela precisa saber do PROCESSAMENTO já na primeira carga.

    Worker parado é invisível até o paciente reclamar — e aí já virou bola de
    neve. Vem junto da saúde do canal porque é a mesma faixa que mostra as
    duas coisas.
    """
    from apps.core.health import registrar_batimento

    registrar_batimento()
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.get("/api/v1/conversations/counters/")

    assert resposta.status_code == 200
    assert resposta.data["processing"]["alive"] is True
    assert "queued" in resposta.data["processing"]


# ─────────────── porta de saída do canal desconectado ─────────────── #


def _derruba(canal, motivo="Credencial expirada"):
    from django.utils import timezone

    canal.disconnected_at = timezone.now()
    canal.disconnect_reason = motivo
    canal.auth_error_count = 2
    canal.save(update_fields=["disconnected_at", "disconnect_reason", "auth_error_count"])


def test_verificar_cura_o_canal_quando_a_credencial_volta(
    api_client, manager_single_clinic, inbox_a
):
    """
    A porta de saída do impasse.

    O canal só se cura com uma chamada bem-sucedida, mas com ele caído o envio
    e o reenvio ficam bloqueados. Sem uma sonda que NÃO seja envio, trocar o
    token não adiantava — o sistema recusava tudo para sempre esperando um
    sucesso que ele mesmo impedia de acontecer. (Achado ao vivo em 29/07.)
    """
    canal = inbox_a["channel"]
    _derruba(canal)
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.post("/api/v1/conversations/check-channel/")

    assert resposta.status_code == 200
    assert resposta.data["ok"] is True
    canal.refresh_from_db()
    assert canal.disconnected is False
    assert canal.auth_error_count == 0
    # A tela recebe a saúde NOVA na mesma resposta: a faixa some sem F5.
    assert resposta.data["channel"]["disconnected"] is False


def test_verificar_com_credencial_ainda_recusada_devolve_o_motivo_da_meta(
    api_client, manager_single_clinic, inbox_a, monkeypatch
):
    """Frase genérica mandaria a recepção adivinhar; o motivo da Meta diz se o
    problema é token, número ou permissão."""
    from apps.integrations.whatsapp.exceptions import WhatsAppAuthError

    class _Recusado:
        def verify_credentials(self):
            raise WhatsAppAuthError("Session has expired on 29-Jul-26 06:00 PDT")

    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider", lambda c: _Recusado()
    )
    canal = inbox_a["channel"]
    _derruba(canal)
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.post("/api/v1/conversations/check-channel/")

    assert resposta.data["ok"] is False
    assert "Session has expired" in resposta.data["detail"]
    canal.refresh_from_db()
    assert canal.disconnected is True


def test_verificar_sem_canal_configurado_explica(
    api_client, manager_single_clinic, clinic_a
):
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.post("/api/v1/conversations/check-channel/")

    assert resposta.status_code == 400
    assert "não tem canal" in str(resposta.data)
