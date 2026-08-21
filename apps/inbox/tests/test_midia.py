"""
Mídia recebida (RF-INB-6, fatia 3): o que o paciente manda tem de chegar na
tela inteiro — nome, extensão, duração, desenho de onda e, quando dá errado,
o motivo.

O áudio destes testes é GERADO com ffmpeg, não é um .ogg guardado no repo: um
arquivo binário no versionamento vira peso morto que ninguém sabe se ainda
corresponde ao que o teste afirma. Gerando aqui, a rampa de volume é conhecida
e a asserção sobre a onda pode ser específica.
"""

import subprocess
from datetime import timedelta

import pytest
from django.utils import timezone
from rest_framework.test import APIClient

from apps.inbox.choices import MediaState, MessageKind, ReactionActor, SenderKind
from apps.inbox.models import MediaAsset, Message, MessageReaction
from apps.inbox.tasks import _nome_do_arquivo, fetch_media_asset
from apps.integrations.whatsapp.base import DownloadedMedia
from apps.integrations.whatsapp.events import parse_meta_webhook


@pytest.fixture
def logado(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


def _audio_ogg(*, segundos=2, rampa=True) -> bytes:
    """Áudio Opus de verdade. Com `rampa`, o volume CRESCE do início ao fim —
    é o que permite afirmar que a onda desenhada acompanha o som."""
    formula = f"0.9*sin(1000*t)*t/{segundos}" if rampa else "0"
    resultado = subprocess.run(
        [
            "ffmpeg", "-v", "error",
            "-f", "lavfi",
            "-i", f"aevalsrc={formula}:d={segundos}",
            "-c:a", "libopus", "-f", "ogg", "pipe:1",
        ],
        capture_output=True,
        check=True,
    )
    return resultado.stdout


class _ProvedorFalso:
    """Dublê no MESMO contrato do adapter: devolve DownloadedMedia ou explode.
    Dublê que inventa contrato é como bug passa despercebido aqui."""

    def __init__(self, *, conteudo=b"", mime="", erro=None):
        self.resposta = DownloadedMedia(content=conteudo, mime_type=mime)
        self.erro = erro

    def download_media(self, media_id):
        if self.erro:
            raise self.erro
        return self.resposta


def _midia(clinic, **kwargs):
    return MediaAsset.objects.create(clinic=clinic, provider_media_id="MID-1", **kwargs)


def _mensagem(clinic, conversation, **kwargs):
    from django.utils import timezone

    kwargs.setdefault("sender_kind", SenderKind.CONTACT)
    kwargs.setdefault("wa_timestamp", timezone.now())
    return Message.objects.create(clinic=clinic, conversation=conversation, **kwargs)


def _com_provedor(monkeypatch, provedor):
    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider",
        lambda channel: provedor,
    )


# ──────────────────────────── parser e extensão ────────────────────────────


def test_nome_do_documento_sobrevive_ao_parser():
    """O nome é o que o paciente vê no celular dele. Sem ele, o exame chega na
    recepção como '1037387288883307.pdf' e ninguém sabe o que está abrindo."""
    payload = {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [
                                {"wa_id": "5589999228477", "profile": {"name": "Gabriel"}}
                            ],
                            "messages": [
                                {
                                    "from": "5589999228477",
                                    "id": "wamid.DOC",
                                    "timestamp": "1785000000",
                                    "type": "document",
                                    "document": {
                                        "id": "MID-DOC",
                                        "mime_type": "application/pdf",
                                        "filename": "encaminhamento-dra-lima.pdf",
                                        "caption": "segue o pedido",
                                    },
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }

    evento = parse_meta_webhook(payload)[0]

    assert evento.filename == "encaminhamento-dra-lima.pdf"
    assert evento.caption == "segue o pedido"
    assert evento.media_id == "MID-DOC"


@pytest.mark.parametrize(
    ("mime", "esperado"),
    [
        # O áudio de voz do WhatsApp. `guess_extension` devolve None para
        # audio/ogg e o arquivo era salvo SEM extensão nenhuma — o navegador
        # recebia algo que não sabia tocar.
        ("audio/ogg; codecs=opus", ".ogg"),
        ("audio/ogg", ".ogg"),
        ("audio/mp4", ".m4a"),
        ("audio/amr", ".amr"),
        ("image/jpeg", ".jpg"),
        ("video/mp4", ".mp4"),
        ("application/pdf", ".pdf"),
    ],
)
def test_extensao_do_arquivo_salvo(clinic_a, mime, esperado):
    media = _midia(clinic_a, mime_type=mime)

    assert _nome_do_arquivo(media).endswith(esperado)


@pytest.mark.parametrize(
    ("arquivo", "esperado"),
    [
        # ⚠️ O áudio de voz do WhatsApp. A imagem do container não traz o
        # `/etc/mime.types`, então `guess_type` devolvia None e o arquivo era
        # SERVIDO como `application/octet-stream` - com esse tipo nenhum player
        # toca, e foi assim que todo áudio recebido parou de tocar na clínica
        # real (21/08/2026). Salvar com a extensão certa não bastava: quem
        # decide o tipo da RESPOSTA é esta tabela.
        ("recado.ogg", "audio/ogg"),
        ("recado.oga", "audio/ogg"),
        ("recado.m4a", "audio/mp4"),  # iPhone
        ("recado.amr", "audio/amr"),
        ("exame.docx", "application/vnd.openxmlformats-officedocument."
                       "wordprocessingml.document"),
        ("planilha.xlsx", "application/vnd.openxmlformats-officedocument."
                          "spreadsheetml.sheet"),
        # Estes o Python já sabia - entram para a lista não encolher em
        # silêncio no dia em que alguém mexer nela.
        ("foto.jpg", "image/jpeg"),
        ("video.mp4", "video/mp4"),
        ("laudo.pdf", "application/pdf"),
    ],
)
def test_o_servidor_sabe_o_TIPO_de_cada_arquivo_que_a_clinica_recebe(
    arquivo, esperado
):
    """
    O tipo com que o arquivo SAI na resposta HTTP, que é o que decide se o
    player toca. Sem isto, o áudio virava um download que não abria nem fora
    do navegador.
    """
    import mimetypes

    assert mimetypes.guess_type(arquivo)[0] == esperado



# ──────────────────────────── desenho de onda ────────────────────────────


def test_onda_e_duracao_saem_do_audio_DE_VERDADE(clinic_a, inbox_a, monkeypatch):
    from apps.inbox.audio import WAVEFORM_BARS

    conteudo = _audio_ogg(segundos=2)
    media = _midia(clinic_a, mime_type="audio/ogg; codecs=opus")
    _com_provedor(monkeypatch, _ProvedorFalso(conteudo=conteudo, mime="audio/ogg; codecs=opus"))

    fetch_media_asset(media.pk)

    media.refresh_from_db()
    assert media.state == MediaState.READY
    assert len(media.waveform) == WAVEFORM_BARS
    assert all(0 <= pico <= 100 for pico in media.waveform)
    # O áudio gerado CRESCE de volume: a onda tem de crescer junto. Esta é a
    # asserção que separa onda real de barrinha inventada — qualquer desenho
    # decorativo passaria nas outras.
    assert media.waveform[-1] > media.waveform[0] * 2
    assert 1500 <= media.duration_ms <= 2500


def test_audio_mudo_nao_ganha_onda_falsa(clinic_a, inbox_a, monkeypatch):
    """Silêncio absoluto desenharia uma fileira de barras invisíveis. Sem onda
    é mais honesto — a tela cai na barra simples."""
    media = _midia(clinic_a, mime_type="audio/ogg")
    _com_provedor(monkeypatch, _ProvedorFalso(conteudo=_audio_ogg(rampa=False), mime="audio/ogg"))

    fetch_media_asset(media.pk)

    media.refresh_from_db()
    assert media.state == MediaState.READY
    assert media.waveform == []


def test_imagem_nao_tem_onda_nem_duracao(clinic_a, inbox_a, monkeypatch):
    media = _midia(clinic_a, mime_type="image/jpeg")
    _com_provedor(monkeypatch, _ProvedorFalso(conteudo=b"\xff\xd8\xff\xe0fake", mime="image/jpeg"))

    fetch_media_asset(media.pk)

    media.refresh_from_db()
    # O READY vem primeiro de propósito: sem ele, um download que FALHOU
    # também deixaria onda e duração vazias e o teste passaria sem provar nada.
    assert media.state == MediaState.READY
    assert media.waveform == []
    assert media.duration_ms is None


# ─────────────────────────── estados do download ───────────────────────────


def test_download_sem_conteudo_FALHA_com_motivo(clinic_a, inbox_a, monkeypatch):
    """A URL da Meta expira: mídia velha não vem mais. Antes a task saía calada
    e a bolha ficava 'Baixando…' para sempre."""
    media = _midia(clinic_a, mime_type="image/jpeg")
    _com_provedor(monkeypatch, _ProvedorFalso(conteudo=b""))

    fetch_media_asset(media.pk)

    media.refresh_from_db()
    assert media.state == MediaState.FAILED
    assert media.error  # asserção que pode falhar: motivo vazio reprova


def test_erro_de_rede_no_download_vira_estado_e_nao_silencio(clinic_a, inbox_a, monkeypatch):
    from apps.integrations.whatsapp.exceptions import WhatsAppUnavailableError

    media = _midia(clinic_a, mime_type="image/jpeg")
    _com_provedor(monkeypatch, _ProvedorFalso(erro=WhatsAppUnavailableError("502")))

    fetch_media_asset(media.pk)

    media.refresh_from_db()
    assert media.state == MediaState.FAILED
    assert media.error == "O WhatsApp não respondeu agora. Tente de novo."


def test_token_morto_vira_frase_HUMANA_e_nao_despejo_da_meta(clinic_a, inbox_a, monkeypatch):
    """Aconteceu ao vivo em 28/07: o token temporário da Meta expirou e o
    balão mostraria `ExpiredAccessToken(code=190, ..., fbtrace_id=...)` para a
    recepção. Texto de log não é texto de tela."""
    from apps.integrations.whatsapp.exceptions import WhatsAppAuthError

    media = _midia(clinic_a, mime_type="image/webp")
    _com_provedor(
        monkeypatch,
        _ProvedorFalso(erro=WhatsAppAuthError("ExpiredAccessToken(code=190, fbtrace_id=AXw...)")),
    )

    fetch_media_asset(media.pk)

    media.refresh_from_db()
    assert media.error == "Canal do WhatsApp desconectado. Avise o suporte para reconectar."
    assert "fbtrace" not in media.error


def test_retry_reenfileira_e_volta_para_baixando(clinic_a, inbox_a, logado, monkeypatch):
    chamadas = []
    monkeypatch.setattr(
        "apps.inbox.tasks.fetch_media_asset.delay", lambda pk: chamadas.append(pk)
    )
    media = _midia(clinic_a, mime_type="image/jpeg", state=MediaState.FAILED, error="deu ruim")
    mensagem = _mensagem(
        clinic_a,
        inbox_a["conversation"],
        kind=MessageKind.IMAGE,
        media=media,
    )

    resposta = logado.post(f"/api/v1/messages/{mensagem.pk}/retry-media/")

    assert resposta.status_code == 200
    assert resposta.data["state"] == MediaState.PENDING
    assert resposta.data["error"] == ""
    assert chamadas == [media.pk]


def test_retry_em_mensagem_sem_midia_e_recusado(clinic_a, inbox_a, logado):
    mensagem = _mensagem(
        clinic_a,
        inbox_a["conversation"],
        kind=MessageKind.TEXT,
        body="só texto",
    )

    resposta = logado.post(f"/api/v1/messages/{mensagem.pk}/retry-media/")

    assert resposta.status_code == 400


# ────────────────────────────── API e socket ──────────────────────────────


def test_serializer_devolve_midia_completa_e_URL_ABSOLUTA(clinic_a, inbox_a, logado, monkeypatch):
    media = _midia(clinic_a, mime_type="audio/ogg", filename="")
    _com_provedor(monkeypatch, _ProvedorFalso(conteudo=_audio_ogg(), mime="audio/ogg"))
    fetch_media_asset(media.pk)
    mensagem = _mensagem(
        clinic_a,
        inbox_a["conversation"],
        kind=MessageKind.AUDIO,
        media=media,
    )

    dados = logado.get(f"/api/v1/messages/{mensagem.pk}/").data

    midia = dados["media_asset"]
    # URL relativa funciona no localhost e quebra em qualquer outro lugar: o
    # front pode estar numa origem diferente da API.
    assert midia["url"].startswith("http://")
    assert midia["url"].endswith(".ogg")
    assert midia["state"] == MediaState.READY
    assert midia["duration_ms"] > 0
    assert midia["waveform"]


def test_message_new_leva_midia_e_legenda_SEPARADAS(clinic_a, inbox_a):
    """Corpo e legenda iam fundidos num campo só e a tela não sabia se aquele
    texto era a mensagem ou a legenda da foto — foto virava texto."""
    from apps.inbox.realtime import _message_min

    media = _midia(clinic_a, mime_type="image/jpeg", state=MediaState.PENDING)
    mensagem = _mensagem(
        clinic_a,
        inbox_a["conversation"],
        kind=MessageKind.IMAGE,
        caption="esse é o pedido da recepção",
        media=media,
    )

    payload = _message_min(mensagem)

    assert payload["body"] == ""
    assert payload["caption"] == "esse é o pedido da recepção"
    # Mesmos nomes do REST: `media` é o id, `media_asset` é o objeto. Já foram
    # a mesma chave com sentidos diferentes e o cliente estourava o cast.
    assert payload["media"] == media.pk
    assert payload["media_asset"]["state"] == MediaState.PENDING
    assert payload["media_asset"]["mime_type"] == "image/jpeg"


def test_midia_pronta_avisa_a_tela(clinic_a, inbox_a, monkeypatch):
    """A conversa já está aberta com o 'Baixando…' girando: sem este evento a
    imagem só apareceria no próximo F5."""
    avisos = []
    monkeypatch.setattr(
        "apps.inbox.realtime.notify_media_updated",
        lambda mensagem, media: avisos.append((mensagem.pk, media.pk)),
    )
    media = _midia(clinic_a, mime_type="image/jpeg")
    mensagem = _mensagem(
        clinic_a,
        inbox_a["conversation"],
        kind=MessageKind.IMAGE,
        media=media,
    )
    _com_provedor(monkeypatch, _ProvedorFalso(conteudo=b"bytes", mime="image/jpeg"))

    fetch_media_asset(media.pk)

    assert avisos == [(mensagem.pk, media.pk)]


def test_midia_de_outra_clinica_nao_vaza_no_retry(clinic_a, clinic_b, inbox_b, api_client, db):
    """Escopo por clínica vale para a mídia como vale para o resto."""
    from apps.accounts.choices import MembershipRole
    from apps.accounts.models import Membership
    from conftest import make_user

    intruso = make_user("intruso.midia@teste.dev")
    Membership.objects.create(user=intruso, clinic=clinic_a, role=MembershipRole.MANAGER)
    media = _midia(clinic_b, mime_type="image/jpeg", state=MediaState.FAILED)
    mensagem = _mensagem(
        clinic_b,
        inbox_b["conversation"],
        kind=MessageKind.IMAGE,
        media=media,
    )

    cliente = APIClient()
    cliente.force_authenticate(intruso)
    resposta = cliente.post(f"/api/v1/messages/{mensagem.pk}/retry-media/")

    assert resposta.status_code == 404
    media.refresh_from_db()
    assert media.state == MediaState.FAILED


# ─────────────────────────── paginação da thread ───────────────────────────


def test_thread_devolve_o_FIM_da_conversa_na_primeira_pagina(clinic_a, inbox_a, logado):
    """
    Achado do usuário em 28/07: abrir a conversa caía na primeira mensagem.

    Em ordem crescente, a página 1 de uma conversa longa traz as mais ANTIGAS —
    quem abre cai no "oi" de meses atrás e o atendimento de hoje só aparece
    depois de rolar tudo. A API devolve do mais novo para o mais antigo, e o
    cliente inverte para desenhar.
    """
    conversation = inbox_a["conversation"]
    for i in range(5):
        _mensagem(
            clinic_a,
            conversation,
            kind=MessageKind.TEXT,
            body=f"mensagem {i}",
            wa_timestamp=timezone.now() - timedelta(minutes=10 - i),
        )

    dados = logado.get(
        "/api/v1/messages/", {"conversation": conversation.pk, "limit": 2}
    ).data

    corpos = [m["body"] for m in dados["results"]]
    # Asserção que PODE falhar: em ordem crescente viria ["mensagem 0", ...].
    assert corpos == ["mensagem 4", "mensagem 3"]
    assert dados["count"] == 5


def test_paginacao_da_thread_nao_repete_nem_pula_no_mesmo_segundo(
    clinic_a, inbox_a, logado
):
    """Rajada de mídia chega com o MESMO carimbo de tempo. Sem desempate por
    id, a ordem varia entre uma página e outra — e a rolagem para cima repete
    balões ou some com eles."""
    conversation = inbox_a["conversation"]
    instante = timezone.now()
    for i in range(6):
        _mensagem(
            clinic_a,
            conversation,
            kind=MessageKind.TEXT,
            body=f"rajada {i}",
            wa_timestamp=instante,
        )

    primeira = logado.get(
        "/api/v1/messages/", {"conversation": conversation.pk, "limit": 3}
    ).data
    segunda = logado.get(
        "/api/v1/messages/",
        {"conversation": conversation.pk, "limit": 3, "offset": 3},
    ).data

    ids = [m["id"] for m in primeira["results"]] + [m["id"] for m in segunda["results"]]
    assert len(set(ids)) == 6, "houve repetição ou salto entre as páginas"


# ───────────────────────────────── reações ─────────────────────────────────


def _payload_de_reacao(*, wa_id, alvo_wamid, emoji, mid="wamid.REACT-1"):
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": wa_id, "profile": {"name": "Gabriel"}}],
                            "messages": [
                                {
                                    "from": wa_id,
                                    "id": mid,
                                    "timestamp": "1785290675",
                                    "type": "reaction",
                                    "reaction": {"emoji": emoji, "message_id": alvo_wamid},
                                }
                            ],
                        }
                    }
                ]
            }
        ]
    }


def test_reacao_cola_na_mensagem_e_NAO_vira_balao(clinic_a, inbox_a):
    """
    Achado ao vivo em 28/07: o usuário reagiu com 👍❤️😢 e cada reação virou um
    balão "Não suportado" na thread, subindo a conversa na fila. Reação é um
    selo na mensagem, não uma fala — e joinha não pede resposta.
    """
    from apps.inbox.services import ingest_events

    conversation = inbox_a["conversation"]
    alvo = _mensagem(
        clinic_a,
        conversation,
        kind=MessageKind.TEXT,
        body="Confirmo sua consulta para amanhã",
        provider_message_id="wamid.ALVO",
    )
    antes = Message.objects.filter(conversation=conversation).count()

    ingest_events(
        inbox_a["channel"],
        parse_meta_webhook(
            _payload_de_reacao(
                wa_id=inbox_a["contact"].wa_id, alvo_wamid="wamid.ALVO", emoji="👍"
            )
        ),
    )

    assert [r.emoji for r in alvo.reactions.all()] == ["👍"]
    assert alvo.reactions.first().actor_kind == ReactionActor.CONTACT
    assert Message.objects.filter(conversation=conversation).count() == antes, (
        "a reação não pode criar balão"
    )


def test_remover_a_reacao_limpa_o_selo(clinic_a, inbox_a):
    """O WhatsApp manda o mesmo evento com emoji VAZIO ao desfazer."""
    from apps.inbox.services import ingest_events

    alvo = _mensagem(
        clinic_a,
        inbox_a["conversation"],
        kind=MessageKind.TEXT,
        body="oi",
        provider_message_id="wamid.ALVO",
    )
    MessageReaction.objects.create(
        clinic=clinic_a,
        message=alvo,
        conversation=inbox_a["conversation"],
        actor_kind=ReactionActor.CONTACT,
        emoji="❤️",
    )

    ingest_events(
        inbox_a["channel"],
        parse_meta_webhook(
            _payload_de_reacao(
                wa_id=inbox_a["contact"].wa_id, alvo_wamid="wamid.ALVO", emoji=""
            )
        ),
    )

    assert not alvo.reactions.exists()


def test_reacao_a_mensagem_que_nao_temos_e_ignorada(clinic_a, inbox_a):
    """Reação a uma conversa anterior à integração: não há onde colar o selo,
    e inventar um balão seria pior do que ignorar."""
    from apps.inbox.services import ingest_events

    antes = Message.objects.count()

    ingest_events(
        inbox_a["channel"],
        parse_meta_webhook(
            _payload_de_reacao(
                wa_id=inbox_a["contact"].wa_id, alvo_wamid="wamid.NUNCA-VISTO", emoji="👍"
            )
        ),
    )

    assert Message.objects.count() == antes


# ────────────────── tipos que a Meta manda e nós lemos agora ──────────────────


def _payload(mensagem: dict, wa_id="5589999228477"):
    mensagem = {"from": wa_id, "id": "wamid.X1", "timestamp": "1785290675", **mensagem}
    return {
        "entry": [
            {
                "changes": [
                    {
                        "value": {
                            "contacts": [{"wa_id": wa_id, "profile": {"name": "Gabriel"}}],
                            "messages": [mensagem],
                        }
                    }
                ]
            }
        ]
    }


def test_cartao_de_contato_vira_nome_e_telefone_sem_repetir():
    """O cartão REAL que chegou trazia o mesmo número duas vezes — etiquetado
    'Antigo' e 'CELL'. Duas linhas iguais na tela pareceriam defeito nosso."""
    evento = parse_meta_webhook(
        _payload(
            {
                "type": "contacts",
                "contacts": [
                    {
                        "name": {
                            "first_name": "Willian",
                            "last_name": "Negreiros",
                            "formatted_name": "Willian Negreiros",
                        },
                        "phones": [
                            {"type": "Antigo", "phone": "+55 89 98119-1501", "wa_id": "558981191501"},
                            {"type": "CELL", "phone": "+55 89 98119-1501", "wa_id": "558981191501"},
                        ],
                    }
                ],
            }
        )
    )[0]

    assert evento.message_kind == MessageKind.CONTACT
    assert evento.body == "Willian Negreiros"
    cartao = evento.content_data["contacts"][0]
    assert cartao["name"] == "Willian Negreiros"
    assert len(cartao["phones"]) == 1, "o mesmo número não pode aparecer duas vezes"
    assert cartao["phones"][0]["wa_id"] == "558981191501"


def test_localizacao_guarda_COORDENADAS_e_nao_so_texto():
    """O wacrm vira tudo em texto 'nome - endereço - lat,long'; ficamos com o
    desenho do Chatwoot, que preserva as coordenadas — texto não abre mapa."""
    evento = parse_meta_webhook(
        _payload(
            {
                "type": "location",
                "location": {
                    "latitude": -5.0892,
                    "longitude": -42.8016,
                    "name": "Clínica MedEssence",
                    "address": "Av. Frei Serafim, 2352 — Centro, Teresina",
                    "url": "https://maps.google.com/?q=-5.0892,-42.8016",
                },
            }
        )
    )[0]

    local = evento.content_data["location"]
    assert (local["latitude"], local["longitude"]) == (-5.0892, -42.8016)
    assert local["title"] == "Clínica MedEssence, Av. Frei Serafim, 2352 — Centro, Teresina"
    assert local["url"]
    assert evento.body == local["title"], "a prévia da fila mostra o rótulo"


def test_resposta_de_botao_guarda_titulo_E_id():
    """Os dois repositórios usam o título como corpo; o wacrm guarda também o
    id — é por ele que o motor de jornadas saberá o caminho escolhido, porque
    o TEXTO do botão muda a cada template."""
    evento = parse_meta_webhook(
        _payload(
            {
                "type": "interactive",
                "interactive": {
                    "type": "button_reply",
                    "button_reply": {"id": "confirmar_consulta", "title": "Confirmar"},
                },
            }
        )
    )[0]

    assert evento.message_kind == MessageKind.INTERACTIVE
    assert evento.body == "Confirmar"
    assert evento.content_data["interactive_id"] == "confirmar_consulta"


def test_botao_de_template_e_tratado_como_resposta_de_botao():
    evento = parse_meta_webhook(
        _payload(
            {
                "type": "button",
                "button": {"text": "Quero remarcar", "payload": "remarcar"},
            }
        )
    )[0]

    assert evento.message_kind == MessageKind.INTERACTIVE
    assert evento.body == "Quero remarcar"
    assert evento.content_data["interactive_id"] == "remarcar"


@pytest.mark.parametrize("tipo", ["ephemeral", "request_welcome"])
def test_tipos_de_ruido_nao_viram_evento(tipo):
    """Regra do Chatwoot: mensagem temporária e 'abriu a conversa sem escrever'
    não têm conteúdo — balão vazio seria ruído na fila. O payload cru continua
    no WebhookEvent, então nada se perde para investigação."""
    assert parse_meta_webhook(_payload({"type": tipo})) == []


def test_unsupported_da_meta_vira_balao_de_indisponivel(clinic_a, inbox_a):
    """A Meta devolveu 131051 'Message type is currently not supported'. O
    Chatwoot persiste um placeholder para a conversa não ficar com buraco
    silencioso — e é o que fazemos."""
    from apps.inbox.services import ingest_events

    ingest_events(
        inbox_a["channel"],
        parse_meta_webhook(
            _payload(
                {
                    "type": "unsupported",
                    "errors": [
                        {
                            "code": 131051,
                            "title": "Message type unknown",
                            "error_data": {"details": "Message type is currently not supported."},
                        }
                    ],
                },
                wa_id=inbox_a["contact"].wa_id,
            )
        ),
    )

    mensagem = Message.objects.filter(conversation=inbox_a["conversation"]).latest("id")
    assert mensagem.kind == MessageKind.UNSUPPORTED
    assert mensagem.body == "", "o texto da tela é do FRONT; aqui fica o tipo"


def test_reacao_de_CADA_ator_convive_na_mesma_mensagem(clinic_a, inbox_a):
    """O defeito do campo único: a clínica reagindo pelo celular apagava a
    reação do paciente. Uma linha por ator (desenho do wacrm) resolve."""
    from apps.inbox.services import ingest_events

    alvo = _mensagem(
        clinic_a,
        inbox_a["conversation"],
        kind=MessageKind.TEXT,
        body="Confirmado!",
        provider_message_id="wamid.ALVO",
    )
    wa_id = inbox_a["contact"].wa_id

    # O paciente reage...
    ingest_events(
        inbox_a["channel"],
        parse_meta_webhook(_payload_de_reacao(wa_id=wa_id, alvo_wamid="wamid.ALVO", emoji="❤️")),
    )
    # ...e a clínica reage pelo celular (chega como ECHO).
    eco = parse_meta_webhook(
        _payload_de_reacao(
            wa_id=wa_id, alvo_wamid="wamid.ALVO", emoji="👍", mid="wamid.REACT-ECO"
        )
    )[0]
    from dataclasses import replace

    from apps.integrations.whatsapp.base import WhatsAppEventKind

    ingest_events(inbox_a["channel"], [replace(eco, kind=WhatsAppEventKind.ECHO)])

    selos = {r.actor_kind: r.emoji for r in alvo.reactions.all()}
    assert selos == {ReactionActor.CONTACT: "❤️", ReactionActor.AGENT: "👍"}


def test_reagir_de_novo_TROCA_o_emoji_do_mesmo_ator(clinic_a, inbox_a):
    from apps.inbox.services import ingest_events

    alvo = _mensagem(
        clinic_a,
        inbox_a["conversation"],
        kind=MessageKind.TEXT,
        body="oi",
        provider_message_id="wamid.ALVO",
    )
    wa_id = inbox_a["contact"].wa_id
    for emoji in ("❤️", "😢"):
        ingest_events(
            inbox_a["channel"],
            parse_meta_webhook(
                _payload_de_reacao(wa_id=wa_id, alvo_wamid="wamid.ALVO", emoji=emoji)
            ),
        )

    assert [r.emoji for r in alvo.reactions.all()] == ["😢"]


def test_reacao_acha_a_mensagem_pelo_SUFIXO_do_wamid(clinic_a, inbox_a):
    """Plano B copiado do whatomate: o WhatsApp codifica o telefone no prefixo
    do wamid, então o id de volta pode não bater caractere a caractere —
    acontece com mensagem enviada pelo celular da clínica."""
    from apps.inbox.services import ingest_events

    alvo = _mensagem(
        clinic_a,
        inbox_a["conversation"],
        kind=MessageKind.TEXT,
        body="enviada pelo celular",
        provider_message_id="wamid.HBgMNTU4OTk5MjI4NDc3FQIAERgSMEEyOEI3RTk1RDcyRkE5RDZC",
    )

    ingest_events(
        inbox_a["channel"],
        parse_meta_webhook(
            _payload_de_reacao(
                wa_id=inbox_a["contact"].wa_id,
                # Mesmo sufixo, prefixo diferente.
                alvo_wamid="wamid.OUTROPREFIXO=MEEyOEI3RTk1RDcyRkE5RDZC",
                emoji="👍",
            )
        ),
    )

    assert [r.emoji for r in alvo.reactions.all()] == ["👍"]


def test_reagir_desfazer_e_reagir_DE_NOVO(clinic_a, inbox_a):
    """A armadilha do soft delete do projeto: linha "apagada" continua ocupando
    a chave única, e a segunda reação estouraria IntegrityError."""
    from apps.inbox.services import ingest_events

    alvo = _mensagem(
        clinic_a,
        inbox_a["conversation"],
        kind=MessageKind.TEXT,
        body="oi",
        provider_message_id="wamid.ALVO",
    )
    wa_id = inbox_a["contact"].wa_id
    for emoji in ("👍", "", "❤️"):
        ingest_events(
            inbox_a["channel"],
            parse_meta_webhook(
                _payload_de_reacao(wa_id=wa_id, alvo_wamid="wamid.ALVO", emoji=emoji)
            ),
        )

    assert [r.emoji for r in alvo.reactions.all()] == ["❤️"]
