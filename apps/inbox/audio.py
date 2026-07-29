"""
Leitura do arquivo de mídia: duração e desenho de onda REAIS.

Por que no servidor e não no navegador: o cálculo é caro e o resultado é o
mesmo para todo mundo. Fazendo aqui, decodifica-se uma vez no download e cada
aba de cada atendente recebe uma lista de números pronta. Fazendo no cliente,
cada abertura da conversa decodificaria o áudio de novo — e o navegador do
Safari nem decodifica Opus, que é justamente o formato do áudio do WhatsApp.

Por que ffmpeg: o áudio do WhatsApp é `audio/ogg; codecs=opus` e os arquivos
encaminhados vêm em mp3, m4a e amr. Uma biblioteca por formato seria um
zoológico; o ffmpeg lê todos. Quando ele não está instalado, estas funções
devolvem vazio e a tela cai na barra simples — o desenho de onda some, nada
quebra.

O que NÃO se faz aqui: inventar barrinhas. Uma onda que não corresponde ao som
é pior do que não ter onda nenhuma, porque parece informação.
"""

import contextlib
import json
import logging
import os
import subprocess
import tempfile

logger = logging.getLogger(__name__)

# Quantas barras o desenho tem. É o número que a tela desenha; mudar aqui muda
# o desenho de todas as mídias baixadas DEPOIS — as antigas guardam o tamanho
# com que foram calculadas, e a tela lida com qualquer tamanho.
WAVEFORM_BARS = 48

# Teto de tempo para não deixar um arquivo estranho segurar o worker de mídia.
FFMPEG_TIMEOUT_SECS = 20


def _run(args: list[str]) -> bytes | None:
    """Roda o binário. `None` quando não deu para rodar."""
    try:
        resultado = subprocess.run(  # noqa: S603 - args fixos, sem shell
            args,
            capture_output=True,
            timeout=FFMPEG_TIMEOUT_SECS,
            check=False,
        )
    except FileNotFoundError:
        # Sem ffmpeg na imagem: não é erro da mídia, é ausência de ferramenta.
        return None
    except subprocess.TimeoutExpired:
        logger.warning("ffmpeg estourou o tempo lendo mídia")
        return None
    if resultado.returncode != 0:
        return None
    return resultado.stdout


@contextlib.contextmanager
def _arquivo(conteudo: bytes):
    """
    Os bytes num arquivo de verdade, apagado no fim.

    Por arquivo e não por pipe porque as duas coisas que queremos exigem
    NAVEGAR no arquivo: a duração do Ogg está na última página (por stdin o
    ffprobe devolve `{}`), e o mp4 com o índice no fim nem decodifica em
    fluxo. Foi exatamente assim que a duração voltou vazia na primeira versão.
    """
    caminho = None
    try:
        with tempfile.NamedTemporaryFile(delete=False, suffix=".media") as tmp:
            tmp.write(conteudo)
            caminho = tmp.name
        yield caminho
    finally:
        if caminho:
            with contextlib.suppress(OSError):
                os.unlink(caminho)


def duracao_ms(caminho: str) -> int | None:
    """Duração real do áudio/vídeo, em milissegundos."""
    saida = _run(
        [
            "ffprobe",
            "-v",
            "error",
            "-show_entries",
            "format=duration",
            "-of",
            "json",
            caminho,
        ]
    )
    if not saida:
        return None
    try:
        segundos = float(json.loads(saida)["format"]["duration"])
    except (ValueError, KeyError, TypeError, json.JSONDecodeError):
        return None
    return int(segundos * 1000)


def desenho_de_onda(caminho: str, *, barras: int = WAVEFORM_BARS) -> list[int]:
    """
    Picos de volume (0..100), um por barra.

    Decodifica para PCM 16 bits mono e tira o PICO de cada fatia — não a média.
    Média achata o áudio inteiro numa linha morna; pico é o que o olho lê como
    "aqui a pessoa falou mais alto", que é o desenho que se reconhece.

    Devolve `[]` quando não deu para decodificar (formato exótico, ffmpeg
    ausente, arquivo corrompido): a tela então mostra a barra simples.
    """
    if barras < 1:
        return []
    pcm = _run(
        [
            "ffmpeg",
            "-v",
            "error",
            "-i",
            caminho,
            "-ac",
            "1",  # mono: duas ondas não cabem numa barra só
            "-ar",
            "8000",  # 8 kHz basta de sobra para desenhar 48 barras
            "-f",
            "s16le",
            "pipe:1",
        ]
    )
    if not pcm or len(pcm) < 2:
        return []

    amostras = memoryview(pcm).cast("h")  # int16 little-endian
    total = len(amostras)
    if total < barras:
        return []

    tamanho = total // barras
    picos: list[int] = []
    for i in range(barras):
        fatia = amostras[i * tamanho : (i + 1) * tamanho]
        # max/min na memoryview rodam em C; percorrer amostra a amostra em
        # Python levaria segundos num áudio de poucos minutos.
        picos.append(max(max(fatia), -min(fatia)))

    teto = max(picos)
    if teto == 0:
        # Silêncio absoluto: uma onda toda em zero desapareceria na tela. Sem
        # onda é mais honesto do que uma linha de barras invisíveis.
        return []
    # Normaliza pelo próprio pico: áudio gravado baixinho tem de desenhar
    # igual ao gravado alto — a onda mostra o RITMO da fala, não o volume do
    # microfone de quem gravou.
    return [max(2, round(pico * 100 / teto)) for pico in picos]


def converter_para_opus(conteudo: bytes) -> bytes | None:
    """
    O áudio gravado no navegador no formato que a Meta aceita.

    O Chrome só grava `audio/webm`, que a Cloud API recusa. O wacrm resolve
    embarcando uma biblioteca de Opus no cliente; aqui converte-se no
    servidor, onde o ffmpeg já está por causa do desenho de onda — uma
    dependência a menos no front, e funciona igual em qualquer navegador.

    `audio/ogg` com Opus é o formato da nota de voz do WhatsApp: assim o
    balão chega no celular do paciente com onda e botão de play, e não como
    anexo genérico. Devolve `None` quando não deu para converter, e aí quem
    chama recusa o envio em vez de mandar um arquivo que a Meta rejeitaria.
    """
    with _arquivo(conteudo) as caminho:
        return _run(
            [
                "ffmpeg",
                "-v",
                "error",
                "-i",
                caminho,
                "-ac",
                "1",  # voz é mono; estéreo só dobraria o tamanho
                "-c:a",
                "libopus",
                "-b:a",
                "32k",  # fala inteligível com arquivo pequeno
                "-f",
                "ogg",
                "pipe:1",
            ]
        )


def ler_metadados(conteudo: bytes, mime: str) -> tuple[int | None, list[int]]:
    """(duração_ms, onda) para o que for áudio ou vídeo; (None, []) no resto."""
    if mime.startswith("audio/"):
        with _arquivo(conteudo) as caminho:
            return duracao_ms(caminho), desenho_de_onda(caminho)
    if mime.startswith("video/"):
        # Vídeo mostra duração no chip da capa, mas não desenha onda: quem
        # olha um vídeo lê a imagem, não o áudio dele.
        with _arquivo(conteudo) as caminho:
            return duracao_ms(caminho), []
    return None, []
