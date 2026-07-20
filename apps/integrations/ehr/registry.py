from apps.integrations.ehr.base import EHRProvider
from apps.integrations.ehr.exceptions import EHRNotConfiguredError
from apps.tenants.choices import EHRProviderKind


def get_ehr_provider(clinic) -> EHRProvider:
    """
    Resolve o adapter da clínica. Trocar de provedor = escrever um adapter
    e registrá-lo aqui - nada acoplado a terceiro fora deste pacote.
    """
    if not clinic.ehr_provider:
        raise EHRNotConfiguredError(f"Clínica {clinic.slug} não tem provedor de EHR configurado.")

    if clinic.ehr_provider == EHRProviderKind.VSAUDE:
        from apps.integrations.ehr.vsaude.adapter import VSaudeAdapter

        return VSaudeAdapter(clinic)

    if clinic.ehr_provider == EHRProviderKind.FAKE:
        from apps.integrations.ehr.fake.adapter import FakeAdapter

        return FakeAdapter(clinic)

    raise EHRNotConfiguredError(f"Provedor desconhecido: {clinic.ehr_provider}")
