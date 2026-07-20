from django.db.models import (
    RESTRICT,
    SET_NULL,
    BooleanField,
    CharField,
    ForeignKey,
    UniqueConstraint,
)

from apps.accounts.choices import MembershipRole
from apps.core.models import BaseModel


class Membership(BaseModel):
    """
    Vínculo user↔clinic - a autorização do plano clínica (§3.1).

    `is_active=False` revoga o acesso preservando o histórico.
    `practitioner` liga o papel de médico à sua carteira/agenda (M3).
    """

    user = ForeignKey(
        "accounts.User",
        verbose_name="Usuário",
        on_delete=RESTRICT,
        related_name="memberships",
    )
    clinic = ForeignKey(
        "tenants.Clinic",
        verbose_name="Clínica",
        on_delete=RESTRICT,
        related_name="memberships",
    )
    role = CharField(
        verbose_name="Papel",
        max_length=20,
        choices=MembershipRole.choices,
    )
    is_active = BooleanField(verbose_name="Ativo", default=True)
    practitioner = ForeignKey(  # M3 - carteira/agenda do médico
        "scheduling.Practitioner",
        verbose_name="Profissional",
        null=True,
        blank=True,
        on_delete=SET_NULL,
        related_name="memberships",
    )

    class Meta:
        verbose_name = "Vínculo"
        verbose_name_plural = "Vínculos"
        constraints = [
            UniqueConstraint(fields=["user", "clinic"], name="uniq_membership_user_clinic"),
        ]

    def __str__(self):
        return f"{self.user} @ {self.clinic} ({self.get_role_display()})"
