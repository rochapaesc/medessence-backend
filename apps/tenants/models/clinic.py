from django.db.models import (
    BooleanField,
    CharField,
    DateField,
    DateTimeField,
    PositiveSmallIntegerField,
    SlugField,
    TextField,
)

from apps.core.fields import EncryptedJSONField
from apps.core.models import BaseModel
from apps.tenants.choices import ClinicStatus, EHRProviderKind, SuspensionCategory


class Clinic(BaseModel):
    """
    O tenant do sistema. Todo modelo de negócio aponta para cá via
    TenantScopedModel; o vínculo com o prontuário externo (EHR) é 1:1
    por clínica, com credenciais cifradas em repouso.
    """

    name = CharField(verbose_name="Nome", max_length=160)
    slug = SlugField(verbose_name="Slug", unique=True)
    status = CharField(
        verbose_name="Situação",
        max_length=12,
        choices=ClinicStatus.choices,
        default=ClinicStatus.ACTIVE,
        help_text=(
            "Suspensa: a equipe não entra, o robô não responde e as sequências "
            "não disparam. As mensagens que o paciente mandar CONTINUAM sendo "
            "recebidas (RF-ADM-1.7)."
        ),
    )
    suspension_category = CharField(
        verbose_name="Motivo da suspensão",
        max_length=20,
        choices=SuspensionCategory.choices,
        blank=True,
    )
    suspension_reason = TextField(
        verbose_name="Detalhe da suspensão",
        max_length=256,
        blank=True,
        help_text="Obrigatório ao suspender: é o que responde 'por quê' meses depois.",
    )
    suspended_at = DateTimeField(
        verbose_name="Suspensa em",
        null=True,
        blank=True,
    )
    timezone = CharField(
        verbose_name="Fuso horário",
        max_length=48,
        default="America/Fortaleza",
        help_text="Base do send_time das jornadas e dos horários exibidos.",
    )
    active_window_days = PositiveSmallIntegerField(
        verbose_name="Janela de paciente ativo (dias)",
        default=90,
        help_text=(
            "Paciente é ATIVO se consultou nos últimos N dias (RF-PAC-2). "
            "Padrão da clínica; cada profissional pode sobrescrever o seu."
        ),
    )
    ehr_provider = CharField(
        verbose_name="Provedor de EHR",
        max_length=20,
        choices=EHRProviderKind.choices,
        blank=True,
        help_text="Vazio = clínica sem integração de prontuário.",
    )
    ehr_credentials = EncryptedJSONField(
        verbose_name="Credenciais do EHR",
        help_text='Ex.: {"api_key": "..."} - cifrado em repouso.',
    )
    ehr_external_tenant_id = CharField(
        verbose_name="ID externo no EHR",
        max_length=64,
        blank=True,
    )
    ehr_push_enabled = BooleanField(
        verbose_name="Escrita no EHR habilitada",
        default=False,
        help_text=(
            "Trava de segurança do write-through: desligada, mutações locais "
            "NÃO são empurradas ao EHR (fase de validação só-leitura). O pull "
            "continua normal. Ligar só depois de validar a leitura."
        ),
    )
    appointments_backfilled_until = DateField(
        verbose_name="Agenda importada até (para trás)",
        null=True,
        blank=True,
        help_text=(
            "Marca-d'água do backfill resumível da agenda: cada sincronização "
            "recua um pedaço e grava aqui, até o limite de histórico. Vazio = "
            "backfill ainda não começou."
        ),
    )

    class Meta:
        verbose_name = "Clínica"
        verbose_name_plural = "Clínicas"

    def __str__(self):
        return self.name

    @property
    def is_suspended(self) -> bool:
        return self.status == ClinicStatus.SUSPENDED
