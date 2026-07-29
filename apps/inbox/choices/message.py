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
    # Cartão de contato (vCard). O paciente encaminha o contato do
    # acompanhante ou de outro paciente — rotina de recepção.
    CONTACT = "contact", "Contato"
    INTERACTIVE = "interactive", "Interativa"
    TEMPLATE = "template", "Template"
    # O WhatsApp entrega assim o que ele mesmo não sabe renderizar para
    # sistemas externos. Vira um balão placeholder honesto, e não um sumiço:
    # conversa com buraco silencioso é pior (regra do Chatwoot).
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


class ReactionActor(TextChoices):
    """Quem colou o selo na mensagem. O contato não tem usuário — ele é o
    outro lado da conversa; o atendente é gente da equipe (e por isso a
    reação dele guarda QUAL pessoa foi)."""

    CONTACT = "contact", "Paciente"
    AGENT = "agent", "Atendente"


class MediaState(TextChoices):
    """
    Onde está o download da mídia (RF-INB-6). Existe porque a tela precisa
    dizer a VERDADE em cada um dos três momentos: a mídia chega por webhook
    mas o arquivo só existe depois que a task baixa da Meta, e a URL de lá
    expira — quando expira, o arquivo nunca vem.

    Sem este campo, "arquivo vazio" significava ao mesmo tempo "espere um
    pouco" e "não vai vir nunca", e a tela mostrava um buraco mudo nos dois
    casos.
    """

    PENDING = "pending", "Baixando"
    READY = "ready", "Pronta"
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
