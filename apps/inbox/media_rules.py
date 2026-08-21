"""
O que a clínica pode ANEXAR, e de onde vem cada teto.

Os limites não são nossos: são os da Cloud API. Validar na hora de escolher o
arquivo é o que o wacrm faz (`PICKER_ACCEPT` com whitelist de mime + teto por
tipo) — sem isso um PDF de 120 MB sobe inteiro para o nosso disco, ocupa banda
da recepção e só falha lá na frente, dentro da chamada da Meta, com um erro em
inglês que ninguém sabe traduzir.

O áudio é o caso especial: o navegador grava no formato que ele quiser (o
Chrome só faz `audio/webm`), e a Meta não aceita webm. Em vez de exigir uma
biblioteca de gravação em Opus no cliente — o caminho do wacrm —, convertemos
no servidor, onde o ffmpeg já está instalado para o desenho de onda.
"""

from apps.inbox.choices import MessageKind

MB = 1024 * 1024

# Tetos da Cloud API. Documento é o maior de longe porque é o que a recepção
# mais manda: laudo, orientação de preparo, guia de convênio.
TETOS = {
    MessageKind.IMAGE: 5 * MB,
    MessageKind.VIDEO: 16 * MB,
    MessageKind.AUDIO: 16 * MB,
    MessageKind.DOCUMENT: 100 * MB,
    MessageKind.STICKER: 100 * 1024,
}

# O que a Meta aceita em cada tipo. Documento fica sem lista: ela aceita
# praticamente qualquer coisa, e uma whitelist só faria a recepção descobrir
# na hora errada que o `.odt` da nutricionista não passa.
MIMES = {
    MessageKind.IMAGE: {"image/jpeg", "image/png"},
    MessageKind.VIDEO: {"video/mp4", "video/3gpp"},
    MessageKind.AUDIO: {
        "audio/aac",
        "audio/amr",
        "audio/mpeg",
        "audio/mp4",
        "audio/ogg",
    },
    MessageKind.STICKER: {"image/webp"},
}

# Áudio que o navegador produz e a Meta recusa. Não é erro do atendente: é a
# gravação do Chrome. Convertemos e seguimos, em vez de mandar refazer.
CONVERTER_PARA_OPUS = {"audio/webm", "audio/wav", "audio/x-wav", "audio/flac"}

# Como o arquivo convertido sai — `audio/ogg` com Opus é o formato da nota de
# voz do WhatsApp, o que faz o balão chegar com onda no celular do paciente em
# vez de virar um anexo de áudio genérico.
MIME_OPUS = "audio/ogg"

# O mesmo formato, declarado como a Cloud API o exige NO ENVIO.
#
# ⚠️ A documentação da Meta é explícita: "audio/ogg (OPUS codecs only; base
# audio/ogg not supported; mono input only)". Subindo como `audio/ogg` puro
# ela ACEITA o upload e devolve um media id — o estrago só aparece do outro
# lado, onde o WhatsApp mostra "Este áudio não está mais disponível. Peça
# para reenviá-lo" com o arquivo intacto aqui dentro. Visto na clínica real em
# 21/08/2026, e é assim que a Meta declara o mime dos áudios que ELA entrega.
MIME_OPUS_ENVIO = "audio/ogg; codecs=opus"


def mime_para_a_meta(mime: str) -> str:
    """O mime como o upload da Cloud API o exige."""
    if (mime or "").split(";")[0].strip().lower() == "audio/ogg":
        return MIME_OPUS_ENVIO
    return mime

ENVIAVEIS = frozenset(TETOS)

# ⚠️ Teto da Cloud API para a LEGENDA de um anexo. Passar dele faz a Meta
# recusar a mensagem inteira, com o arquivo já subido: a recepção perde o
# upload e o texto, e o erro não diz que o problema é o tamanho da legenda.
# O wacrm corta no `maxLength` do compositor; aqui o servidor também cobra,
# porque ele é quem fala com a Meta.
TETO_DA_LEGENDA = 1024

# Áudio e figurinha não levam legenda: a Meta ignora, e mandar mesmo assim
# faz o texto sumir sem aviso nenhum para quem escreveu.
SEM_LEGENDA = frozenset({MessageKind.AUDIO, MessageKind.STICKER})


def tipo_do_arquivo(mime: str) -> str:
    """
    O tipo de mensagem para um mime. Cai em DOCUMENT de propósito: é o balde
    honesto do WhatsApp — o que ele não sabe tocar nem mostrar, ele entrega
    como arquivo, e ninguém perde o anexo por causa de um mime desconhecido.
    """
    mime = (mime or "").split(";", 1)[0].strip().lower()
    if mime == "image/webp":
        return MessageKind.STICKER
    if mime.startswith("image/"):
        return MessageKind.IMAGE
    if mime.startswith("video/"):
        return MessageKind.VIDEO
    if mime.startswith("audio/"):
        return MessageKind.AUDIO
    return MessageKind.DOCUMENT


def humano(bytes_: int) -> str:
    if bytes_ >= MB:
        return f"{bytes_ / MB:.0f} MB"
    return f"{bytes_ / 1024:.0f} KB"


def validar(kind: str, mime: str, tamanho: int) -> None:
    """
    Recusa o que a Meta recusaria, com a frase que explica o limite.

    Levanta `ValueError` — quem chama traduz em 400. O texto é o que a
    recepção lê: dizer "unsupported media type" faria a pessoa tentar de novo
    com o mesmo arquivo.
    """
    if kind not in TETOS:
        raise ValueError("Este tipo de anexo não pode ser enviado pelo WhatsApp.")

    teto = TETOS[kind]
    if tamanho > teto:
        rotulo = MessageKind(kind).label.lower()
        raise ValueError(
            f"O arquivo tem {humano(tamanho)}. O WhatsApp aceita no máximo "
            f"{humano(teto)} para {rotulo}."
        )

    aceitos = MIMES.get(kind)
    mime_base = (mime or "").split(";", 1)[0].strip().lower()
    if aceitos and mime_base not in aceitos and mime_base not in CONVERTER_PARA_OPUS:
        legiveis = ", ".join(sorted(m.split("/")[-1].upper() for m in aceitos))
        raise ValueError(f"Formato não aceito pelo WhatsApp. Use: {legiveis}.")
