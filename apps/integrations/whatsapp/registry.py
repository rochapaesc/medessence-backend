from apps.inbox.choices import WhatsAppProviderKind
from apps.integrations.whatsapp.base import WhatsAppProvider
from apps.integrations.whatsapp.exceptions import WhatsAppNotConfiguredError


def get_whatsapp_provider(channel) -> WhatsAppProvider:
    """
    Resolve o adapter do canal. Trocar de provedor = escrever um adapter e
    registrá-lo aqui — nada acoplado a terceiro fora deste pacote (§5).
    """
    if channel.provider == WhatsAppProviderKind.DATAFY:
        from apps.integrations.whatsapp.datafy.adapter import DatafyAdapter

        return DatafyAdapter(channel)

    if channel.provider == WhatsAppProviderKind.FAKE:
        from apps.integrations.whatsapp.fake.adapter import FakeWhatsAppAdapter

        return FakeWhatsAppAdapter(channel)

    raise WhatsAppNotConfiguredError(f"Provedor desconhecido: {channel.provider}")
