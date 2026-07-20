from django.conf import settings
from django.db.models import SET_NULL, CharField, ForeignKey, PositiveSmallIntegerField

from apps.core.models import TenantScopedModel


class Practitioner(TenantScopedModel):
    """Profissional de saúde da clínica (espelhado do EHR ou local)."""

    name = CharField(verbose_name="Nome", max_length=200)
    active_window_days = PositiveSmallIntegerField(
        verbose_name="Janela de paciente ativo (dias)",
        null=True,
        blank=True,
        help_text=(
            "Sobrescreve o padrão da clínica para a carteira deste profissional "
            "(ex.: dermato 180d). Vazio = usa o da clínica."
        ),
    )
    license_number = CharField(
        verbose_name="Registro (CRM)",
        max_length=120,
        blank=True,
        help_text="licenceNumber do EHR - pode incluir RQE e texto livre.",
    )
    external_id = CharField(verbose_name="ID no EHR", max_length=64, blank=True)
    user = ForeignKey(
        settings.AUTH_USER_MODEL,
        verbose_name="Usuário",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="practitioners",
        help_text="Liga o profissional ao login - carteira do médico.",
    )

    class Meta:
        verbose_name = "Profissional"
        verbose_name_plural = "Profissionais"

    def __str__(self):
        return self.name

    @property
    def effective_active_window_days(self) -> int:
        """Janela efetiva da carteira: override do profissional ou padrão da clínica."""
        return self.active_window_days or self.clinic.active_window_days
