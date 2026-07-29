from django.db.models import (
    CharField,
    FileField,
    JSONField,
    PositiveIntegerField,
)

from apps.core.models import TenantScopedModel
from apps.inbox.choices import MediaState


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

    # O nome que o paciente vê no celular dele. Sem isto, o exame que ele
    # mandou chega na recepção como "1037387288883307.pdf" e ninguém sabe o
    # que está baixando antes de abrir.
    filename = CharField(verbose_name="Nome do arquivo", max_length=255, blank=True)

    state = CharField(
        verbose_name="Estado",
        max_length=8,
        choices=MediaState.choices,
        default=MediaState.PENDING,
    )
    error = CharField(verbose_name="Motivo da falha", max_length=200, blank=True)

    # Áudio e vídeo: duração real, lida do arquivo. A tela mostra "0:07" antes
    # de tocar — sem isso ninguém sabe se são 5 segundos ou 5 minutos.
    duration_ms = PositiveIntegerField(verbose_name="Duração (ms)", null=True, blank=True)

    # Picos de volume REAIS (0..100), um por barra do desenho de onda. Ficam no
    # banco porque é caro: calcular uma vez no download é melhor do que cada
    # aba de cada atendente decodificar o áudio de novo. Fica vazio quando não
    # deu para ler o áudio — aí a tela desenha a barra simples, que é honesta,
    # em vez de barrinhas inventadas que não correspondem ao som.
    waveform = JSONField(verbose_name="Onda", default=list, blank=True)

    class Meta:
        verbose_name = "Mídia"
        verbose_name_plural = "Mídias"

    def __str__(self):
        return (
            self.filename
            or self.stored_file.name
            or self.provider_media_id
            or f"Mídia {self.pk}"
        )
