from django.db.models import (
    BooleanField,
    CharField,
    DateField,
    PositiveSmallIntegerField,
    SlugField,
)

from apps.core.fields import EncryptedJSONField
from apps.core.models import BaseModel
from apps.tenants.choices import EHRProviderKind


class Clinic(BaseModel):
    """
    O tenant do sistema. Todo modelo de negócio aponta para cá via
    TenantScopedModel; o vínculo com o prontuário externo (EHR) é 1:1
    por clínica, com credenciais cifradas em repouso.
    """

    name = CharField(verbose_name="Nome", max_length=160)
    slug = SlugField(verbose_name="Slug", unique=True)
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
