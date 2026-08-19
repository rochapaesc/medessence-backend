from django.db.models import TextChoices


class WhatsAppProviderKind(TextChoices):
    """Provedor do canal WhatsApp (§5). META fala com a Cloud API oficial,
    direto (decisão de 27/07/2026 — o proxy Datafy foi descartado); FAKE
    alimenta o desenvolvimento sem número real (mesmo padrão do EHR)."""

    META = "meta", "Meta Cloud API"
    FAKE = "fake", "Fake (desenvolvimento)"


class ChannelSource(TextChoices):
    """
    Por onde o canal foi ligado (RF-CON-2.4).

    Muda o que a tela oferece: reconectar pelo popup da Meta só faz sentido
    para quem entrou por ele. Canal colado à mão (`manage.py wa_channel`)
    continua existindo para calibração e para clínica migrando de provedor.
    """

    EMBEDDED_SIGNUP = "embedded_signup", "Cadastro incorporado da Meta"
    MANUAL = "manual", "Configurado à mão"
