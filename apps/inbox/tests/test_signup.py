"""
Conexão do canal pelo cadastro incorporado (§4.3.3, F2.7, fatia A).

O que estes testes prendem, e por quê: a ORDEM das chamadas à Meta (assinar o
webhook antes de gravar), o fato de o token nunca voltar para a tela, a
reconexão reaproveitando o canal, e a recusa do número que já é de outra
clínica. Cada um deles corresponde a uma falha que seria silenciosa em produção.
"""

import httpx
import pytest
from django.utils import timezone

from apps.inbox.choices import ChannelSource, WhatsAppProviderKind
from apps.inbox.models import Channel
from apps.integrations.whatsapp.exceptions import (
    WhatsAppAuthError,
    WhatsAppError,
    WhatsAppUnavailableError,
)
from apps.integrations.whatsapp.meta import signup as meta_signup

CHANNEL = "/api/v1/channel/"
CONFIG = "/api/v1/channel/signup-config/"
CONNECT = "/api/v1/channel/connect/"
DISCONNECT = "/api/v1/channel/disconnect/"

TOKEN = "EAAG-token-do-cliente"
WABA = "102938475601122"
PHONE_ID = "109876543210987"


class MetaFalsa:
    """
    Dublê da Graph no nível do `httpx.request`, que é a fronteira real.

    Dublar as nossas próprias funções esconderia justamente o que precisa ser
    verificado: a ordem em que elas são chamadas e o que vai em cada uma.
    """

    def __init__(self, **respostas):
        self.chamadas = []
        self.respostas = respostas

    def __call__(self, metodo, url, **kwargs):
        self.chamadas.append((metodo, url, kwargs))
        for pedaco, resposta in self.respostas.items():
            if pedaco in url:
                return resposta
        return _resposta(200, {})

    @property
    def caminhos(self):
        return [url.split("/v25.0/")[-1].split("?")[0] for _, url, _ in self.chamadas]


def _resposta(status_code, payload):
    return httpx.Response(
        status_code,
        json=payload,
        request=httpx.Request("POST", "https://graph.facebook.com/"),
    )


def _meta_ok(**ajustes):
    numero = {
        "id": PHONE_ID,
        "display_phone_number": "+55 89 98119-1501",
        "verified_name": "Instituto MedEssence",
        "code_verification_status": "VERIFIED",
        **ajustes,
    }
    return MetaFalsa(
        **{
            "oauth/access_token": _resposta(200, {"access_token": TOKEN}),
            "phone_numbers": _resposta(200, {"data": [numero]}),
            "subscribed_apps": _resposta(200, {"success": True}),
        }
    )


@pytest.fixture
def app_da_meta(settings):
    """O app da plataforma configurado, que é pré-requisito de tudo (P19)."""
    settings.WHATSAPP_APP_ID = "1131225510080233"
    settings.WHATSAPP_APP_SECRET = "segredo-que-nao-sai-do-servidor"
    settings.WHATSAPP_CONFIG_ID = "1796569444642013"
    settings.WHATSAPP_GRAPH_VERSION = "v25.0"
    return settings


@pytest.fixture
def gestor(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


def _conectar(client, monkeypatch, meta=None, **corpo):
    meta = meta or _meta_ok()
    monkeypatch.setattr(httpx, "request", meta)
    resposta = client.post(
        CONNECT,
        {"code": "AQD-codigo-do-popup", "waba_id": WABA, "business_id": "77", **corpo},
        format="json",
    )
    return resposta, meta


# --------------------------------------------------------------------- #
# O caminho feliz
# --------------------------------------------------------------------- #


def test_conectar_grava_o_canal_da_clinica(gestor, monkeypatch, app_da_meta, clinic_a):
    resposta, _ = _conectar(gestor, monkeypatch)

    assert resposta.status_code == 201
    canal = Channel.objects.get(clinic=clinic_a, is_test=False)
    assert canal.provider == WhatsAppProviderKind.META
    assert canal.phone_number_id == PHONE_ID
    assert canal.waba_id == WABA
    assert canal.display_number == "+55 89 98119-1501"
    assert canal.verified_name == "Instituto MedEssence"
    assert canal.credentials["access_token"] == TOKEN
    assert canal.connection_source == ChannelSource.EMBEDDED_SIGNUP
    assert canal.is_coexistence, "o fluxo é o de coexistência (RF-CON-3)"
    assert canal.connected_at is not None


def test_a_ordem_das_chamadas_a_meta(gestor, monkeypatch, app_da_meta):
    """
    RF-CON-2: trocar o código, descobrir o número, assinar o webhook.

    ⚠️ Assinar é o passo cuja falta não acusa nada: o canal ficaria salvo e
    MUDO. Por isso a ordem é teste, e não comentário.
    """
    _, meta = _conectar(gestor, monkeypatch)

    assert meta.caminhos == [
        "oauth/access_token",
        f"{WABA}/phone_numbers",
        f"{WABA}/subscribed_apps",
    ]


def test_o_segredo_do_app_vai_na_troca_e_nao_no_resto(gestor, monkeypatch, app_da_meta):
    """RF-CON-1.2: o `client_secret` só existe na troca do código, e sai daqui."""
    _, meta = _conectar(gestor, monkeypatch)

    troca, numeros, assinatura = meta.chamadas
    assert troca[2]["params"]["client_secret"] == "segredo-que-nao-sai-do-servidor"
    assert "client_secret" not in numeros[2].get("params", {})
    # As chamadas seguintes usam o token DO CLIENTE, não o segredo do app.
    assert numeros[2]["params"]["access_token"] == TOKEN
    assert assinatura[2]["headers"]["Authorization"] == f"Bearer {TOKEN}"


def test_a_resposta_NAO_devolve_credencial(gestor, monkeypatch, app_da_meta):
    """
    RF-CON-1.1: o gestor nunca vê credencial, e o token não volta nem para ser
    guardado no navegador. O fluxo de referência o imprime na tela.
    """
    resposta, _ = _conectar(gestor, monkeypatch)

    corpo = str(resposta.data)
    assert TOKEN not in corpo
    assert WABA not in corpo, "waba_id é identificador da Meta, não vai para a tela"
    assert resposta.data["canal"]["display_number"] == "+55 89 98119-1501"


def test_o_numero_do_popup_vence_a_lista(gestor, monkeypatch, app_da_meta, clinic_a):
    """Conta com dois números: liga o que a Meta disse, não o primeiro."""
    outro = {"id": "999", "display_phone_number": "+55 11 4002-8922", "verified_name": "Outro"}
    meta = MetaFalsa(
        **{
            "oauth/access_token": _resposta(200, {"access_token": TOKEN}),
            "phone_numbers": _resposta(
                200,
                {"data": [outro, {"id": PHONE_ID, "display_phone_number": "+55 89 98119-1501"}]},
            ),
            "subscribed_apps": _resposta(200, {}),
        }
    )

    _conectar(gestor, monkeypatch, meta=meta, phone_number_id=PHONE_ID)

    assert Channel.objects.get(clinic=clinic_a).phone_number_id == PHONE_ID


# --------------------------------------------------------------------- #
# Reconexão (RF-CON-2.5)
# --------------------------------------------------------------------- #


def test_reconectar_REAPROVEITA_o_canal(gestor, monkeypatch, app_da_meta, clinic_a, inbox_a):
    """
    Canal novo levaria junto conversas, mensagens e execuções de fluxo, que
    apontam para o registro antigo. E esbarraria na trava de um por clínica.
    """
    canal_antigo = inbox_a["channel"]

    resposta, _ = _conectar(gestor, monkeypatch)

    assert resposta.status_code == 201
    assert Channel.objects.filter(clinic=clinic_a, is_test=False).count() == 1
    canal_antigo.refresh_from_db()
    assert canal_antigo.phone_number_id == PHONE_ID
    assert canal_antigo.conversations.count() == 1, "a conversa continua no lugar"


def test_reconectar_CURA_o_canal_caido(gestor, monkeypatch, app_da_meta, inbox_a):
    """Quem acabou de reautorizar não pode continuar vendo a faixa de caído."""
    canal = inbox_a["channel"]
    canal.auth_error_count = 2
    canal.disconnected_at = timezone.now()
    canal.disconnect_reason = "A Meta recusou as credenciais."
    canal.save()

    _conectar(gestor, monkeypatch)

    canal.refresh_from_db()
    assert not canal.disconnected
    assert canal.auth_error_count == 0
    assert canal.disconnect_reason == ""


def test_reconectar_PRESERVA_o_app_secret_do_canal(
    gestor, monkeypatch, app_da_meta, inbox_a
):
    """
    ⚠️ O `app_secret` por canal (WABA compartilhado) mora no mesmo dicionário.
    Sobrescrever `credentials` inteiro tiraria a conferência de assinatura do
    webhook do ar, e a clínica pararia de RECEBER logo depois de reconectar.
    """
    canal = inbox_a["channel"]
    canal.credentials = {"access_token": "velho", "app_secret": "segredo-do-canal"}
    canal.save()

    _conectar(gestor, monkeypatch)

    canal.refresh_from_db()
    assert canal.credentials["access_token"] == TOKEN
    assert canal.credentials["app_secret"] == "segredo-do-canal"


# --------------------------------------------------------------------- #
# Recusas
# --------------------------------------------------------------------- #


def test_numero_de_OUTRA_clinica_e_recusado(
    gestor, monkeypatch, app_da_meta, clinic_b, clinic_a
):
    """
    RF-CON-2.6: sem isto, o webhook entregaria a conversa de uma clínica dentro
    da outra, em silêncio (o `filter().first()` pegaria qualquer um dos dois).
    """
    Channel.objects.create(
        clinic=clinic_b,
        provider=WhatsAppProviderKind.META,
        phone_number_id=PHONE_ID,
    )

    resposta, meta = _conectar(gestor, monkeypatch)

    assert resposta.status_code == 409
    assert resposta.data["code"] == "numero_em_outra_clinica"
    assert not Channel.objects.filter(clinic=clinic_a).exists()
    assert f"{WABA}/subscribed_apps" not in meta.caminhos, (
        "recusar antes de assinar: assinar o webhook de um número que não vamos "
        "usar mexeria na conta do cliente à toa"
    )


def test_a_meta_recusando_NAO_deixa_canal_pela_metade(
    gestor, monkeypatch, app_da_meta, clinic_a
):
    """Canal gravado sem token pareceria conectado e recusaria toda mensagem."""
    meta = MetaFalsa(
        **{
            "oauth/access_token": _resposta(
                400,
                {"error": {"message": "Invalid verification code format.", "code": 100}},
            )
        }
    )

    resposta, _ = _conectar(gestor, monkeypatch, meta=meta)

    assert resposta.status_code == 400
    assert resposta.data["code"] == "meta_recusou"
    assert not Channel.objects.filter(clinic=clinic_a).exists()


def test_falha_ao_assinar_o_webhook_NAO_grava_canal(
    gestor, monkeypatch, app_da_meta, clinic_a
):
    """
    O canal mudo é o pior desfecho possível: tudo parece certo na tela e nada
    chega. Melhor a clínica continuar sem canal e tentar de novo.
    """
    meta = _meta_ok()
    meta.respostas["subscribed_apps"] = _resposta(
        403, {"error": {"message": "Permissions error", "code": 200}}
    )

    resposta, _ = _conectar(gestor, monkeypatch, meta=meta)

    assert resposta.status_code == 400
    assert not Channel.objects.filter(clinic=clinic_a).exists()


def test_conta_SEM_numero_explica_o_que_fazer(gestor, monkeypatch, app_da_meta):
    meta = _meta_ok()
    meta.respostas["phone_numbers"] = _resposta(200, {"data": []})

    resposta, _ = _conectar(gestor, monkeypatch, meta=meta)

    assert resposta.status_code == 400
    assert "não tem nenhum número" in resposta.data["detail"]


def test_popup_incompleto_pede_para_tentar_de_novo(gestor, app_da_meta):
    """RF-CON-1.4: `code` e `waba_id` chegam por caminhos diferentes."""
    resposta = gestor.post(CONNECT, {"code": "AQD-x"}, format="json")

    assert resposta.status_code == 400
    assert resposta.data["code"] == "signup_incompleto"
    assert resposta.data["faltando"] == ["waba_id"]


def test_plataforma_sem_app_configurado_diz_que_nao_da(gestor, settings, monkeypatch):
    """P19: sem app da Meta, o botão explica em vez de abrir popup quebrado."""
    settings.WHATSAPP_APP_ID = ""
    settings.WHATSAPP_APP_SECRET = ""
    settings.WHATSAPP_CONFIG_ID = ""

    resposta, _ = _conectar(gestor, monkeypatch)

    assert resposta.status_code == 503
    assert resposta.data["code"] == "app_nao_configurado"


# --------------------------------------------------------------------- #
# Quem pode
# --------------------------------------------------------------------- #


def test_atendente_NAO_conecta(api_client, attendant_a, app_da_meta):
    api_client.force_authenticate(attendant_a)
    resposta = api_client.post(CONNECT, {"code": "x", "waba_id": WABA}, format="json")
    assert resposta.status_code == 403


def test_atendente_LE_o_estado_do_canal(api_client, attendant_a, inbox_a):
    """Quem está com o paciente na linha precisa saber que o WhatsApp caiu."""
    api_client.force_authenticate(attendant_a)
    resposta = api_client.get(CHANNEL)
    assert resposta.status_code == 200
    assert resposta.data["conectado"] is True
    # Canal de pé: o carimbo de queda sai nulo, não some do payload.
    assert resposta.data["canal"]["disconnected_at"] is None


def test_canal_caido_diz_DESDE_QUANDO(gestor, inbox_a):
    """
    RF-CFG-4.1: "caiu" sem o "há quanto tempo" obriga a adivinhar a urgência.
    O modelo sempre teve `disconnected_at`; o payload é que o engolia.
    """
    canal = inbox_a["channel"]
    canal.auth_error_count = 2
    canal.disconnected_at = timezone.now()
    canal.disconnect_reason = "Session has expired."
    canal.save()

    resposta = gestor.get(CHANNEL)

    assert resposta.status_code == 200
    assert resposta.data["conectado"] is False
    assert resposta.data["canal"]["disconnected_at"] is not None
    assert resposta.data["canal"]["disconnect_reason"] == "Session has expired."


def test_atendente_nao_ve_a_configuracao_do_app(api_client, attendant_a, app_da_meta):
    api_client.force_authenticate(attendant_a)
    assert api_client.get(CONFIG).status_code == 403


# --------------------------------------------------------------------- #
# Estado, configuração e desconexão
# --------------------------------------------------------------------- #


def test_clinica_sem_canal(gestor):
    resposta = gestor.get(CHANNEL)
    assert resposta.status_code == 200
    assert resposta.data == {"conectado": False, "canal": None}


def test_configuracao_do_popup_NAO_leva_o_segredo(gestor, app_da_meta):
    resposta = gestor.get(CONFIG)

    assert resposta.status_code == 200
    assert resposta.data["disponivel"] is True
    assert resposta.data["app_id"] == "1131225510080233"
    assert resposta.data["config_id"] == "1796569444642013"
    assert "secret" not in str(resposta.data).lower()


def test_configuracao_avisa_quando_a_plataforma_nao_esta_pronta(gestor, settings):
    settings.WHATSAPP_APP_ID = ""
    settings.WHATSAPP_CONFIG_ID = ""

    assert gestor.get(CONFIG).data["disponivel"] is False


def test_desconectar_NAO_apaga_nada(gestor, inbox_a, clinic_a):
    """A clínica que desliga hoje e religa amanhã reencontra as conversas."""
    resposta = gestor.post(DISCONNECT)

    assert resposta.status_code == 200
    assert resposta.data["conectado"] is False
    canal = Channel.objects.get(clinic=clinic_a, is_test=False)
    assert canal.disconnected
    assert canal.conversations.count() == 1
    assert canal.credentials is not None


def test_desconectar_sem_canal_e_404(gestor):
    assert gestor.post(DISCONNECT).status_code == 404


# --------------------------------------------------------------------- #
# A camada que fala com a Meta
# --------------------------------------------------------------------- #


def test_erro_de_rede_e_indisponibilidade_e_nao_recusa(app_da_meta, monkeypatch):
    """
    Rede caindo é transitório. Traduzir para "a Meta recusou" mandaria o gestor
    procurar problema na conta dele.
    """
    def cai(*args, **kwargs):
        raise httpx.ConnectTimeout("estourou")

    monkeypatch.setattr(httpx, "request", cai)

    with pytest.raises(WhatsAppUnavailableError):
        meta_signup.trocar_codigo_por_token("AQD-x")


def test_token_expirado_vira_erro_de_credencial(app_da_meta, monkeypatch):
    monkeypatch.setattr(
        httpx,
        "request",
        lambda *a, **k: _resposta(401, {"error": {"message": "expired", "code": 190}}),
    )

    with pytest.raises(WhatsAppAuthError):
        meta_signup.trocar_codigo_por_token("AQD-x")


def test_a_meta_sem_token_na_resposta_e_erro(app_da_meta, monkeypatch):
    """200 com corpo vazio existe na Graph, e gravaria canal sem credencial."""
    monkeypatch.setattr(httpx, "request", lambda *a, **k: _resposta(200, {}))

    with pytest.raises(WhatsAppError):
        meta_signup.trocar_codigo_por_token("AQD-x")


def test_mensagem_para_o_usuario_final_vence_a_tecnica(app_da_meta, monkeypatch):
    """
    A Meta manda `error_user_title`/`error_user_msg` traduzidos quando o texto
    é para quem está na tela. Mostrar o `message` cru foi defeito real em 13/08.
    """
    monkeypatch.setattr(
        httpx,
        "request",
        lambda *a, **k: _resposta(
            400,
            {
                "error": {
                    "message": "(#100) Invalid parameter",
                    "error_user_title": "Conta indisponível",
                    "error_user_msg": "Esta conta do WhatsApp já está em uso.",
                }
            },
        ),
    )

    with pytest.raises(WhatsAppError) as erro:
        meta_signup.trocar_codigo_por_token("AQD-x")
    assert "Conta indisponível. Esta conta do WhatsApp já está em uso." in str(erro.value)
