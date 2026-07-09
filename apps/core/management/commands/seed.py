"""
Seed de desenvolvimento — IDEMPOTENTE.

Rodar N vezes produz o mesmo estado: os registros são resolvidos por chave
natural (slug da clínica, email do usuário) via get_or_create, e o Faker é
semeado para gerar sempre os mesmos nomes.

Uso:
    python manage.py seed
    python manage.py seed --clinics 3 --doctors 2 --attendants 1
    python manage.py seed --only clinics
    python manage.py seed --only users,memberships

Credenciais criadas (senha padrão: medessence123):
    admin@medessence.dev            → admin da plataforma
    gestor@medessence.dev           → MANAGER em TODAS as clínicas (testa o seletor)
    medico{n}.{slug}@medessence.dev → DOCTOR por clínica
    atendente{n}.{slug}@medessence.dev → ATTENDANT por clínica
"""

from django.core.management.base import BaseCommand, CommandError
from faker import Faker

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership, User
from apps.tenants.models import Clinic

SECTIONS = ("clinics", "users", "memberships")
DEFAULT_PASSWORD = "medessence123"


class Command(BaseCommand):
    help = "Popula dados de desenvolvimento (idempotente, Faker pt_BR)."

    def add_arguments(self, parser):
        parser.add_argument(
            "--only",
            default="",
            help=f"Seções separadas por vírgula: {', '.join(SECTIONS)}. Vazio = todas.",
        )
        parser.add_argument("--clinics", type=int, default=2, help="Quantidade de clínicas.")
        parser.add_argument("--doctors", type=int, default=2, help="Médicos por clínica.")
        parser.add_argument("--attendants", type=int, default=1, help="Atendentes por clínica.")
        parser.add_argument(
            "--password", default=DEFAULT_PASSWORD, help="Senha dos usuários de seed."
        )

    def handle(self, *args, **options):
        only = [s.strip() for s in options["only"].split(",") if s.strip()]
        invalid = set(only) - set(SECTIONS)
        if invalid:
            raise CommandError(
                f"Seções inválidas: {', '.join(sorted(invalid))}. Use: {', '.join(SECTIONS)}"
            )
        sections = only or list(SECTIONS)

        fake = Faker("pt_BR")
        Faker.seed(20260709)  # determinístico — mesmos nomes em toda execução

        clinics = (
            self._seed_clinics(fake, options["clinics"])
            if "clinics" in sections
            else list(Clinic.objects.all())
        )

        if "users" in sections or "memberships" in sections:
            self._seed_users_and_memberships(
                fake,
                clinics,
                doctors=options["doctors"],
                attendants=options["attendants"],
                password=options["password"],
                create_memberships="memberships" in sections,
            )

        self.stdout.write(self.style.SUCCESS("Seed concluído."))

    # ------------------------------------------------------------------ #

    def _seed_clinics(self, fake, quantity) -> list[Clinic]:
        clinics = []
        for index in range(1, quantity + 1):
            slug = f"clinica-{index}"
            name = f"Clínica {fake.last_name()}"
            clinic, created = Clinic.objects.get_or_create(
                slug=slug,
                defaults={"name": name, "timezone": "America/Fortaleza"},
            )
            clinics.append(clinic)
            self._log("Clinic", clinic.slug, created)
        return clinics

    def _seed_users_and_memberships(
        self, fake, clinics, *, doctors, attendants, password, create_memberships
    ):
        # Admin da plataforma — não tem Membership (plano administrativo)
        self._get_or_create_user(
            fake,
            email="admin@medessence.dev",
            password=password,
            is_platform_admin=True,
        )

        # Gestor com vínculo em TODAS as clínicas — exercita o seletor do front
        manager = self._get_or_create_user(fake, email="gestor@medessence.dev", password=password)
        if create_memberships:
            for clinic in clinics:
                self._get_or_create_membership(manager, clinic, MembershipRole.MANAGER)

        for clinic in clinics:
            for index in range(1, doctors + 1):
                doctor = self._get_or_create_user(
                    fake, email=f"medico{index}.{clinic.slug}@medessence.dev", password=password
                )
                if create_memberships:
                    self._get_or_create_membership(doctor, clinic, MembershipRole.DOCTOR)

            for index in range(1, attendants + 1):
                attendant = self._get_or_create_user(
                    fake, email=f"atendente{index}.{clinic.slug}@medessence.dev", password=password
                )
                if create_memberships:
                    self._get_or_create_membership(attendant, clinic, MembershipRole.ATTENDANT)

    def _get_or_create_user(self, fake, *, email, password, **extra) -> User:
        user = User.objects.filter(email=email).first()
        if user is None:
            user = User.objects.create_user(
                email=email,
                password=password,
                first_name=fake.first_name(),
                last_name=fake.last_name(),
                **extra,
            )
            self._log("User", email, created=True)
        else:
            self._log("User", email, created=False)
        return user

    def _get_or_create_membership(self, user, clinic, role):
        membership, created = Membership.objects.get_or_create(
            user=user,
            clinic=clinic,
            defaults={"role": role},
        )
        self._log("Membership", f"{user.email} @ {clinic.slug} ({role})", created)
        return membership

    def _log(self, resource, key, created):
        marker = self.style.SUCCESS("+ criado") if created else self.style.NOTICE("= existente")
        self.stdout.write(f"  {marker}  {resource}: {key}")
