from django.db.models import TextChoices


class EHRProviderKind(TextChoices):
    VSAUDE = "vsaude", "vSaúde"
    # Gerador determinístico para desenvolvimento - exercita o pull completo
    # sem depender de API externa. Inofensivo em produção (ninguém o configura).
    FAKE = "fake", "Fake (desenvolvimento)"
