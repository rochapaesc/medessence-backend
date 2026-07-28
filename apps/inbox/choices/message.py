from django.db.models import TextChoices


class MessageDirection(TextChoices):
    """Sentido da mensagem (§9.11). Derivado de `sender_kind` no `save()` -
    `CONTACT`→`IN`, `AGENT`/`BOT`→`OUT`; nunca aceito cru do cliente."""

    IN = "in", "Recebida"
    OUT = "out", "Enviada"


class SenderKind(TextChoices):
    """Quem originou a mensagem (§9.11). Fonte da verdade para a direção."""

    CONTACT = "contact", "Contato"
    AGENT = "agent", "Atendente"
    BOT = "bot", "Automação"
    # Evento de atividade (RF-ATD-4): não tem autor humano nem sai do sistema.
    SYSTEM = "system", "Sistema"


class MessageKind(TextChoices):
    """Tipo de conteúdo (§9.11). `UNSUPPORTED` cobre o que o WhatsApp entrega
    e ainda não tratamos (mantém a thread íntegra)."""

    TEXT = "text", "Texto"
    IMAGE = "image", "Imagem"
    AUDIO = "audio", "Áudio"
    VIDEO = "video", "Vídeo"
    DOCUMENT = "document", "Documento"
    STICKER = "sticker", "Figurinha"
    LOCATION = "location", "Localização"
    INTERACTIVE = "interactive", "Interativa"
    TEMPLATE = "template", "Template"
    UNSUPPORTED = "unsupported", "Não suportado"
    # Evento na linha do tempo (RF-ATD-4): nem mensagem, nem nota.
    ACTIVITY = "activity", "Evento"


class MessageStatus(TextChoices):
    """Ciclo de entrega de mensagens OUT, atualizado pelos `statuses` do
    webhook (§9.11). Vazio nas mensagens IN."""

    SENT = "sent", "Enviada"
    DELIVERED = "delivered", "Entregue"
    READ = "read", "Lida"
    FAILED = "failed", "Falhou"


# Mapa canônico direção ← sender_kind (M8): a direção nunca diverge do autor.
SENDER_TO_DIRECTION = {
    SenderKind.CONTACT: MessageDirection.IN,
    SenderKind.AGENT: MessageDirection.OUT,
    SenderKind.BOT: MessageDirection.OUT,
    # Evento de atividade não tem direção: não entrou nem saiu do WhatsApp.
    # Fica OUT porque o campo não aceita vazio, e a tela filtra por `kind`.
    SenderKind.SYSTEM: MessageDirection.OUT,
}
