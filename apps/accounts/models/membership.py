from django.core.exceptions import ValidationError
from django.db.models import (
    RESTRICT,
    SET_NULL,
    BooleanField,
    CharField,
    ForeignKey,
    Q,
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
            # Uma carteira tem UM dono: dois vínculos vivos não apontam para o
            # mesmo profissional (a agenda/carteira dele ficaria compartilhada).
            UniqueConstraint(
                fields=["clinic", "practitioner"],
                condition=Q(deleted_at__isnull=True, practitioner__isnull=False),
                name="uniq_membership_clinic_practitioner",
            ),
        ]

    def clean(self):
        """
        O profissional do vínculo TEM de ser da mesma clínica (M3). Sem isto,
        trocar a clínica de um vínculo no admin carrega o profissional antigo
        junto e a carteira do médico vem vazia na API (`practitioner` inválido
        no filtro) - foi exatamente o que aconteceu em 21/07/2026.
        """
        super().clean()
        if self.practitioner_id is None:
            return

        if self.clinic_id and self.practitioner.clinic_id != self.clinic_id:
            raise ValidationError(
                {
                    "practitioner": (
                        f"'{self.practitioner.name}' é profissional de outra clínica "
                        f"({self.practitioner.clinic}). Escolha um profissional de "
                        f"{self.clinic}."
                    )
                }
            )
        if self.role != MembershipRole.DOCTOR:
            raise ValidationError(
                {
                    "practitioner": (
                        "Só o papel Médico tem profissional vinculado "
                        "(a carteira/agenda dele)."
                    )
                }
            )

        taken = (
            Membership.objects.filter(
                clinic_id=self.clinic_id, practitioner_id=self.practitioner_id
            )
            .exclude(pk=self.pk)
            .select_related("user")
            .first()
        )
        if taken is not None:
            raise ValidationError(
                {
                    "practitioner": (
                        f"'{self.practitioner.name}' já é a carteira de "
                        f"{taken.user.email} nesta clínica."
                    )
                }
            )

    def __str__(self):
        return f"{self.user} @ {self.clinic} ({self.get_role_display()})"
