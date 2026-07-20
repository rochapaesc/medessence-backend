"""
Linha do tempo clínica (espelho do EHR + registros locais).

Uma tabela única (`ClinicalEntry`) cobre os 4 discriminators do prontuário
vSaúde (docs/vsaude-clinico.md) e o CRUD local do sistema standalone:
    - origin=EHR   → espelho read-only (pull sobrescreve; UI não edita);
    - origin=LOCAL → criado aqui (nota/exame/prescrição da clínica).

Exames vêm de DUAS fontes no EHR (ExamMedicalRecord com link e
ExaminationService com descrição) → mesmo kind=exam, diferenciados por
`source`, com unicidade (clinic, source, external_id).
"""

from django.db.models import (
    SET_NULL,
    BooleanField,
    CharField,
    DateTimeField,
    ForeignKey,
    Index,
    JSONField,
    Q,
    TextChoices,
    TextField,
    UniqueConstraint,
    URLField,
)

from apps.core.choices import SyncStatus
from apps.core.models import TenantScopedModel


class ClinicalEntryKind(TextChoices):
    NOTE = "note", "Nota"
    PRESCRIPTION = "prescription", "Prescrição"
    EXAM = "exam", "Exame"
    FORM_RESPONSE = "form_response", "Formulário"


class ClinicalOrigin(TextChoices):
    EHR = "ehr", "EHR (espelho)"
    LOCAL = "local", "Local"


class ClinicalEntry(TenantScopedModel):
    patient = ForeignKey(
        "patients.Patient",
        verbose_name="Paciente",
        on_delete=SET_NULL,
        null=True,
        related_name="clinical_entries",
    )
    practitioner = ForeignKey(
        "scheduling.Practitioner",
        verbose_name="Profissional",
        on_delete=SET_NULL,
        null=True,
        blank=True,
        related_name="clinical_entries",
    )
    kind = CharField(verbose_name="Tipo", max_length=15, choices=ClinicalEntryKind.choices)
    origin = CharField(
        verbose_name="Origem",
        max_length=8,
        choices=ClinicalOrigin.choices,
        default=ClinicalOrigin.LOCAL,
    )
    date = DateTimeField(verbose_name="Data", db_index=True)
    text = TextField(
        verbose_name="Conteúdo",
        blank=True,
        help_text="HTML sanitizado - texto da nota ou conteúdo da prescrição local.",
    )
    title = CharField(verbose_name="Título", max_length=200, blank=True)
    description = TextField(verbose_name="Descrição", blank=True)
    document_url = URLField(
        verbose_name="Documento",
        max_length=500,
        blank=True,
        help_text="Link do EHR (prescrição/exame) - visualização autenticada.",
    )
    form_answers = JSONField(
        verbose_name="Respostas do formulário",
        default=list,
        blank=True,
        help_text='[{"label", "answer", "answerValue", "fieldType", ...}]',
    )
    creator_name = CharField(verbose_name="Autor (nome)", max_length=200, blank=True)
    creator_license = CharField(verbose_name="Autor (registro)", max_length=120, blank=True)
    source = CharField(
        verbose_name="Fonte",
        max_length=20,
        blank=True,
        help_text="record | examination (EHR); vazio = local.",
    )
    external_id = CharField(verbose_name="ID no EHR", max_length=64, blank=True)
    raw_payload = JSONField(verbose_name="Payload cru", default=dict, blank=True)
    sync_status = CharField(
        verbose_name="Sincronização",
        max_length=10,
        choices=SyncStatus.choices,
        default=SyncStatus.SYNCED,
    )

    class Meta:
        verbose_name = "Registro clínico"
        verbose_name_plural = "Registros clínicos"
        constraints = [
            UniqueConstraint(
                fields=["clinic", "source", "external_id"],
                condition=~Q(external_id=""),
                name="uniq_clinical_entry_ext",
            ),
        ]
        indexes = [Index(fields=["patient", "-date"])]

    def __str__(self):
        return f"{self.get_kind_display()} · {self.patient} · {self.date:%d/%m/%Y}"


class PrescriptionModel(TenantScopedModel):
    """Modelo de prescrição (catálogo) - espelho do EHR ou criado localmente."""

    name = CharField(verbose_name="Nome", max_length=200)
    content = TextField(verbose_name="Conteúdo", blank=True, help_text="HTML sanitizado.")
    hint = TextField(verbose_name="Instruções", blank=True)
    medications = JSONField(verbose_name="Medicações", default=list, blank=True)
    smart = BooleanField(verbose_name="Smart", default=False)
    special_prescription = BooleanField(verbose_name="Receita especial", default=False)
    origin = CharField(
        verbose_name="Origem",
        max_length=8,
        choices=ClinicalOrigin.choices,
        default=ClinicalOrigin.LOCAL,
    )
    external_id = CharField(verbose_name="ID no EHR", max_length=64, blank=True)

    class Meta:
        verbose_name = "Modelo de prescrição"
        verbose_name_plural = "Modelos de prescrição"
        constraints = [
            UniqueConstraint(
                fields=["clinic", "external_id"],
                condition=~Q(external_id=""),
                name="uniq_prescription_model_ext",
            ),
        ]

    def __str__(self):
        return self.name
