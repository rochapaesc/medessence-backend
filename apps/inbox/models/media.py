from django.db.models import (
    CharField,
    FileField,
    PositiveIntegerField,
)

from apps.core.models import TenantScopedModel


class MediaAsset(TenantScopedModel):
    """
    Mídia recebida re-hospedada em storage próprio (RF-INB-6): a URL da Meta
    expira em ~30 dias, então baixamos e guardamos. `provider_media_id` liga
    de volta ao ativo original para o download assíncrono (`fetch_media_asset`).
    """

    provider_media_id = CharField(verbose_name="Media ID (provedor)", max_length=128, blank=True)
    stored_file = FileField(verbose_name="Arquivo", upload_to="wa-media/%Y/%m/", blank=True)
    mime_type = CharField(verbose_name="MIME", max_length=100, blank=True)
    size_bytes = PositiveIntegerField(verbose_name="Tamanho (bytes)", null=True, blank=True)

    class Meta:
        verbose_name = "Mídia"
        verbose_name_plural = "Mídias"

    def __str__(self):
        return self.stored_file.name or self.provider_media_id or f"Mídia {self.pk}"
