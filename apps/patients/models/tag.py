from django.db.models import CASCADE, BigIntegerField, CharField, ForeignKey, Q, UniqueConstraint

from apps.core.choices import SyncStatus
from apps.core.models import BaseModel, TenantScopedModel
from apps.patients.choices import TagOrigin, TagSyncScope


class Tag(TenantScopedModel):
    """
    Catálogo unificado de tags, sincronizado nos dois sentidos com o EHR
    (§10.3). Espelhada do EHR: external_id/identifier preenchidos. Criada
    aqui: nasce PENDING_PUSH; sem capacidade no bitmask de 64 bits do EHR,
    vira LOCAL_ONLY (caso normal, não erro). Cor é atributo local.
    """

    name = CharField(verbose_name="Nome", max_length=120)
    color = CharField(verbose_name="Cor", max_length=7, default="#149AA1")
    external_id = CharField(verbose_name="ID no EHR", max_length=32, blank=True)
    identifier = BigIntegerField(
        verbose_name="Identifier (bitmask)",
        null=True,
        blank=True,
        help_text=(
            "Bit da tag no EHR - chega a 2^62. BigInteger é exato e suficiente: "
            "a vSaúde (.NET) usa long signed de 64 bits, teto em 2^62."
        ),
    )
    sync_scope = CharField(
        verbose_name="Escopo de sync",
        max_length=15,
        choices=TagSyncScope.choices,
        default=TagSyncScope.SYNCED,
    )

    class Meta:
        verbose_name = "Tag"
        verbose_name_plural = "Tags"
        constraints = [
            UniqueConstraint(
                fields=["clinic", "name"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_tag_name",
            ),
        ]

    def __str__(self):
        return self.name


class PatientTag(BaseModel):
    """Atribuição de tag a paciente - bidirecional assíncrona com o EHR."""

    patient = ForeignKey(
        "patients.Patient",
        verbose_name="Paciente",
        on_delete=CASCADE,
        related_name="patient_tags",
    )
    tag = ForeignKey(
        Tag,
        verbose_name="Tag",
        on_delete=CASCADE,
        related_name="assignments",
    )
    origin = CharField(
        verbose_name="Origem",
        max_length=10,
        choices=TagOrigin.choices,
        help_text="O diff do pull só cria/remove atribuições origin=EHR.",
    )
    sync_status = CharField(
        verbose_name="Sincronização",
        max_length=10,
        choices=SyncStatus.choices,
        default=SyncStatus.SYNCED,
    )

    class Meta:
        verbose_name = "Tag do paciente"
        verbose_name_plural = "Tags dos pacientes"
        constraints = [
            # M5 - unicidade entre vivos: o diff do sync pressupõe 1 atribuição por par
            UniqueConstraint(
                fields=["patient", "tag"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_patient_tag",
            ),
        ]

    def __str__(self):
        return f"{self.patient} · {self.tag}"
