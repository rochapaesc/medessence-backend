from django.db.models import (
    RESTRICT,
    BooleanField,
    CharField,
    ForeignKey,
    UniqueConstraint,
)

from apps.accounts.choices import MembershipRole
from apps.core.models import BaseModel


class Membership(BaseModel):
    """
    Vínculo user↔clinic — a autorização do plano clínica (§3.1).

    `is_active=False` revoga o acesso preservando o histórico.
    A FK `practitioner` (carteira do médico) entra na F1, junto com o
    app scheduling.
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

    class Meta:
        verbose_name = "Vínculo"
        verbose_name_plural = "Vínculos"
        constraints = [
            UniqueConstraint(fields=["user", "clinic"], name="uniq_membership_user_clinic"),
        ]

    def __str__(self):
        return f"{self.user} @ {self.clinic} ({self.get_role_display()})"
