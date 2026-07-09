from django.db.models import TextChoices


class EHRProviderKind(TextChoices):
    VSAUDE = "vsaude", "vSaúde"
