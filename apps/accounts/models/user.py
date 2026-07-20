from django.contrib.auth.models import AbstractBaseUser, PermissionsMixin
from django.db.models import BooleanField, CharField, DateTimeField, EmailField

from apps.accounts.managers.user import UserManager


class User(AbstractBaseUser, PermissionsMixin):
    """
    Usuário global - o papel de clínica mora no Membership, nunca aqui.
    O mesmo usuário pode ser gestor numa clínica e médico em outra.
    Paciente não é usuário do sistema.
    """

    first_name = CharField(verbose_name="Primeiro nome", max_length=80)
    last_name = CharField(verbose_name="Sobrenome", max_length=80)
    email = EmailField(verbose_name="Email", unique=True)
    is_platform_admin = BooleanField(
        verbose_name="Admin da plataforma",
        default=False,
        help_text=(
            "Papel `admin` do plano plataforma: métricas cross-clinic, tenants, "
            "planos. NÃO dá acesso a conteúdo privado das clínicas."
        ),
    )
    is_staff = BooleanField(verbose_name="Membro da equipe", default=False)
    is_active = BooleanField(verbose_name="Ativo", default=True)
    created_at = DateTimeField(verbose_name="Criado em", auto_now_add=True)

    objects = UserManager()

    EMAIL_FIELD = "email"
    USERNAME_FIELD = "email"
    REQUIRED_FIELDS = ["first_name", "last_name"]

    class Meta:
        verbose_name = "Usuário"
        verbose_name_plural = "Usuários"

    def __str__(self):
        return self.email

    def get_full_name(self):
        return f"{self.first_name} {self.last_name}".strip()

    def get_short_name(self):
        return self.first_name
