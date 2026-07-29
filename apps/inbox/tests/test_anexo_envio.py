"""
Anexo SAINDO da clínica (Bloco A): subir o arquivo, validar como a Meta
validaria, e mandar pelo caminho no disco.

Os arquivos aqui são gerados, não guardados no repo — mesma regra do
`test_midia.py`: binário versionado vira peso morto que ninguém sabe se ainda
corresponde ao que o teste afirma.
"""

import subprocess
from datetime import timedelta

import pytest
from django.core.files.base import ContentFile
from django.core.files.uploadedfile import SimpleUploadedFile
from django.utils import timezone

from apps.inbox.choices import MediaState, MessageKind
from apps.inbox.models import MediaAsset, Message

MEDIA = "/api/v1/media/"
MESSAGES = "/api/v1/messages/"


def _png(largura=8) -> bytes:
    """PNG de verdade — o mime vem do upload, mas o conteúdo tem de abrir."""
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         f"color=c=red:s={largura}x{largura}:d=1", "-frames:v", "1",
         "-f", "image2", "-c:v", "png", "pipe:1"],
        capture_output=True, check=True,
    ).stdout


def _webm_de_voz(segundos=1) -> bytes:
    """O que o Chrome grava: webm/opus. A Meta recusa — tem de ser convertido."""
    return subprocess.run(
        ["ffmpeg", "-v", "error", "-f", "lavfi", "-i",
         f"aevalsrc=0.6*sin(900*t):d={segundos}",
         "-c:a", "libopus", "-f", "webm", "pipe:1"],
        capture_output=True, check=True,
    ).stdout


def _abre_janela(conversation):
    """Janela de 24h aberta: sem inbound recente, qualquer envio é recusado."""
    conversation.last_inbound_at = timezone.now() - timedelta(minutes=5)
    conversation.save(update_fields=["last_inbound_at"])


@pytest.fixture
def logado(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    return api_client


def _subir(client, conteudo, nome, mime):
    return client.post(
        MEDIA,
        {"file": SimpleUploadedFile(nome, conteudo, content_type=mime)},
        format="multipart",
    )


class _ProvedorEspiao:
    """Guarda o que foi pedido. Mesma assinatura do adapter real — dublê mais
    permissivo esconderia justamente o erro de parâmetro que só a Meta reprova."""

    def __init__(self):
        self.chamadas = []

    def send_media(self, to, kind, url_or_id, caption=None, *, filename=None,
                   mime_type=None, reply_to=None, is_voice=False):
        from apps.integrations.whatsapp.base import SendResult

        self.chamadas.append(
            {"to": to, "kind": kind, "arquivo": url_or_id, "caption": caption,
             "filename": filename, "mime_type": mime_type, "is_voice": is_voice}
        )
        return SendResult(provider_message_id="wamid.ANEXO", raw={})

    def send_text(self, to, body, reply_to=None):  # pragma: no cover - guarda
        raise AssertionError("mensagem com anexo não pode sair como texto")


# ──────────────────────────────── upload ────────────────────────────────


def test_upload_de_imagem_guarda_nome_tamanho_e_estado(logado, inbox_a):
    conteudo = _png()
    resposta = _subir(logado, conteudo, "preparo.png", "image/png")

    assert resposta.status_code == 201
    assert resposta.data["filename"] == "preparo.png"
    assert resposta.data["size_bytes"] == len(conteudo)
    # PRONTA já na resposta: diferente da mídia recebida, esta não passa pela
    # task de download — se nascesse `pending`, a prévia ficaria girando para
    # sempre esperando um evento que nunca vem.
    assert resposta.data["state"] == MediaState.READY
    assert resposta.data["url"]


def test_upload_recusa_arquivo_acima_do_teto_da_meta(logado, inbox_a):
    """O teto é da Cloud API. Recusar aqui é o que evita subir 6 MB para
    descobrir na chamada da Meta, com erro em inglês."""
    gigante = b"\x89PNG\r\n\x1a\n" + b"0" * (6 * 1024 * 1024)
    resposta = _subir(logado, gigante, "enorme.png", "image/png")

    assert resposta.status_code == 400
    texto = str(resposta.data)
    assert "6 MB" in texto and "5 MB" in texto


def test_upload_recusa_formato_que_a_meta_nao_aceita(logado, inbox_a):
    resposta = _subir(logado, b"GIF89a", "engracado.gif", "image/gif")

    assert resposta.status_code == 400
    # A frase diz o que SERVE, não só o que falhou.
    assert "JPEG" in str(resposta.data)


def test_audio_do_navegador_e_convertido_para_o_formato_do_whatsapp(logado, inbox_a):
    """O Chrome só grava webm; a Meta só aceita ogg/opus. Converter no servidor
    evita embarcar uma biblioteca de Opus no front."""
    resposta = _subir(logado, _webm_de_voz(), "gravacao.webm", "audio/webm")

    assert resposta.status_code == 201
    assert resposta.data["mime_type"] == "audio/ogg"
    assert resposta.data["filename"].endswith(".ogg")
    # Duração e onda saem do arquivo aqui: o balão do atendente nasce com
    # "0:01" e com a onda, igual ao que ele vê chegar do paciente.
    assert resposta.data["duration_ms"] > 0
    assert len(resposta.data["waveform"]) > 0


def test_upload_de_outro_tenant_nao_aparece(logado, inbox_a, clinic_b):
    """Escopo: a mídia nasce na clínica ativa, não na do payload."""
    resposta = _subir(logado, _png(), "foto.png", "image/png")
    media = MediaAsset.objects.get(pk=resposta.data["id"])
    assert media.clinic_id != clinic_b.pk


# ───────────────────────────── envio da mensagem ─────────────────────────


def test_mensagem_com_anexo_sai_como_imagem(logado, inbox_a, monkeypatch):
    espiao = _ProvedorEspiao()
    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider", lambda c: espiao
    )
    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    media_id = _subir(logado, _png(), "preparo.png", "image/png").data["id"]

    resposta = logado.post(
        MESSAGES,
        {"conversation": conversation.id, "media": media_id, "caption": "segue o preparo"},
        format="json",
    )

    assert resposta.status_code == 201
    # O tipo vem do ARQUIVO: quem escolhe "foto" e manda PDF tem de virar
    # documento, não uma imagem que a Meta recusaria.
    assert resposta.data["kind"] == MessageKind.IMAGE
    assert espiao.chamadas[0]["kind"] == MessageKind.IMAGE
    assert espiao.chamadas[0]["caption"] == "segue o preparo"
    # Caminho no disco, não URL: o storage da clínica não é público.
    assert espiao.chamadas[0]["arquivo"].startswith("/")


def test_documento_leva_o_nome_original(logado, inbox_a, monkeypatch):
    """Sem o nome, o paciente recebe '3f8a....bin' e não sabe se abre o laudo
    ou o preparo."""
    espiao = _ProvedorEspiao()
    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider", lambda c: espiao
    )
    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    media_id = _subir(
        logado, b"%PDF-1.4 conteudo", "orientacoes.pdf", "application/pdf"
    ).data["id"]

    resposta = logado.post(
        MESSAGES, {"conversation": conversation.id, "media": media_id}, format="json"
    )

    assert resposta.status_code == 201
    assert resposta.data["kind"] == MessageKind.DOCUMENT
    assert espiao.chamadas[0]["filename"] == "orientacoes.pdf"


def test_gravacao_vai_como_nota_de_voz(logado, inbox_a, monkeypatch):
    """`is_voice` é o que faz o balão chegar com onda e play no celular do
    paciente, em vez de anexo de áudio genérico."""
    espiao = _ProvedorEspiao()
    monkeypatch.setattr(
        "apps.integrations.whatsapp.registry.get_whatsapp_provider", lambda c: espiao
    )
    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    media_id = _subir(logado, _webm_de_voz(), "gravacao.webm", "audio/webm").data["id"]

    logado.post(
        MESSAGES, {"conversation": conversation.id, "media": media_id}, format="json"
    )

    assert espiao.chamadas[0]["kind"] == MessageKind.AUDIO
    assert espiao.chamadas[0]["is_voice"] is True


def test_anexo_nao_pode_ser_enviado_duas_vezes(logado, inbox_a):
    """A guarda que impede reencaminhar o exame de um paciente para outro:
    só se anexa mídia que ainda não tem mensagem."""
    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    media_id = _subir(logado, _png(), "foto.png", "image/png").data["id"]

    primeira = logado.post(
        MESSAGES, {"conversation": conversation.id, "media": media_id}, format="json"
    )
    segunda = logado.post(
        MESSAGES, {"conversation": conversation.id, "media": media_id}, format="json"
    )

    assert primeira.status_code == 201
    assert segunda.status_code == 400
    assert "já foi enviado" in str(segunda.data)


def test_anexo_de_outra_clinica_e_recusado(logado, inbox_a, clinic_b):
    conversation = inbox_a["conversation"]
    _abre_janela(conversation)
    alheia = MediaAsset.objects.create(
        clinic=clinic_b, mime_type="image/png", state=MediaState.READY, filename="x.png"
    )
    alheia.stored_file.save("x.png", ContentFile(_png()), save=True)

    resposta = logado.post(
        MESSAGES, {"conversation": conversation.id, "media": alheia.pk}, format="json"
    )

    assert resposta.status_code == 400
    assert Message.objects.filter(media=alheia).count() == 0


def test_anexo_tambem_respeita_a_janela_de_24h(logado, inbox_a):
    """A janela fecha para TUDO que vai ao WhatsApp — anexo não é exceção."""
    conversation = inbox_a["conversation"]
    conversation.last_inbound_at = timezone.now() - timedelta(hours=30)
    conversation.save(update_fields=["last_inbound_at"])
    media_id = _subir(logado, _png(), "foto.png", "image/png").data["id"]

    resposta = logado.post(
        MESSAGES, {"conversation": conversation.id, "media": media_id}, format="json"
    )

    assert resposta.status_code == 400
    assert "24h" in str(resposta.data)
