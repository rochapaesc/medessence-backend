"""
Gestão de vínculos user↔clínica (§3.1) fora do admin - útil para operação,
suporte e para preparar contas de teste por papel.

Uso:
    # lista os vínculos de um usuário (ou de uma clínica)
    manage.py membership list --user medico1.clinica-2@medessence.dev
    manage.py membership list --clinic medessence

    # cria/atualiza o vínculo (idempotente por user+clinic)
    manage.py membership set --user gestor@x.dev --clinic medessence --role manager
    manage.py membership set --user medico@x.dev --clinic medessence \
        --role doctor --practitioner 7
    manage.py membership set --user medico@x.dev --clinic medessence \
        --role doctor --practitioner-name AGAMENON

O vínculo passa por `full_clean()`: profissional de OUTRA clínica ou já usado
por outro médico é recusado aqui, como no admin.
"""

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership, User
from apps.scheduling.models import Practitioner
from apps.tenants.models import Clinic


class Command(BaseCommand):
    help = "Lista, cria ou atualiza vínculos user↔clínica (papel e profissional)."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=["list", "set"])
        parser.add_argument("--user", help="E-mail do usuário.")
        parser.add_argument("--clinic", help="Slug da clínica.")
        parser.add_argument("--role", choices=[r.value for r in MembershipRole])
        parser.add_argument("--practitioner", type=int, help="PK do profissional.")
        parser.add_argument(
            "--practitioner-name",
            help="Trecho do nome do profissional (alternativa ao --practitioner).",
        )
        parser.add_argument(
            "--unlink-practitioner",
            action="store_true",
            help="Remove o profissional do vínculo.",
        )
        parser.add_argument(
            "--inactive", action="store_true", help="Cria/atualiza como is_active=False."
        )
        parser.add_argument(
            "--create-user",
            action="store_true",
            help="Cria o usuário se não existir (exige --password; use --name p/ o nome).",
        )
        parser.add_argument("--password", help="Senha do usuário criado com --create-user.")
        parser.add_argument("--name", help="Nome completo do usuário criado.")

    def handle(self, *args, **options):
        if options["action"] == "list":
            return self._list(options)
        return self._set(options)

    # ------------------------------ list ------------------------------ #

    def _list(self, options):
        memberships = Membership.objects.select_related(
            "user", "clinic", "practitioner"
        ).order_by("user__email", "clinic__slug")
        if options["user"]:
            memberships = memberships.filter(user__email=options["user"])
        if options["clinic"]:
            memberships = memberships.filter(clinic__slug=options["clinic"])
        if not memberships.exists():
            self.stdout.write("Nenhum vínculo encontrado.")
            return

        for m in memberships:
            practitioner = (
                f"{m.practitioner.name} (#{m.practitioner_id}, clínica {m.practitioner.clinic_id})"
                if m.practitioner
                else "—"
            )
            flag = "" if m.is_active else " [INATIVO]"
            self.stdout.write(
                f"#{m.pk} {m.user.email} @ {m.clinic.slug} | {m.role} | "
                f"profissional: {practitioner}{flag}"
            )

    # ------------------------------- set ------------------------------- #

    def _set(self, options):
        if not options["user"] or not options["clinic"]:
            raise CommandError("set exige --user e --clinic.")

        user = User.objects.filter(email=options["user"]).first()
        if user is None:
            if not options["create_user"]:
                raise CommandError(
                    f"Usuário '{options['user']}' não existe (use --create-user)."
                )
            if not options["password"]:
                raise CommandError("--create-user exige --password.")
            first, _, last = (options["name"] or options["user"].split("@")[0]).partition(" ")
            user = User.objects.create_user(
                email=options["user"],
                password=options["password"],
                first_name=first,
                last_name=last or "",
            )
            self.stdout.write(self.style.SUCCESS(f"Usuário criado: {user.email}"))

        try:
            clinic = Clinic.objects.get(slug=options["clinic"])
        except Clinic.DoesNotExist as exc:
            raise CommandError(f"Clínica '{options['clinic']}' não existe.") from exc

        membership = Membership.objects.filter(user=user, clinic=clinic).first()
        if membership is None:
            if not options["role"]:
                raise CommandError("Vínculo novo exige --role.")
            membership = Membership(user=user, clinic=clinic, role=options["role"])
        elif options["role"]:
            membership.role = options["role"]

        if options["unlink_practitioner"]:
            membership.practitioner = None
        elif options["practitioner"] or options["practitioner_name"]:
            membership.practitioner = self._resolve_practitioner(clinic, options)

        membership.is_active = not options["inactive"]

        try:
            membership.full_clean()
        except ValidationError as exc:
            raise CommandError("; ".join(_flatten(exc))) from exc
        membership.save()

        practitioner = membership.practitioner
        self.stdout.write(
            self.style.SUCCESS(
                f"#{membership.pk} {user.email} @ {clinic.slug} | {membership.role} | "
                f"profissional: {practitioner.name if practitioner else '—'}"
            )
        )

    def _resolve_practitioner(self, clinic, options):
        if options["practitioner"]:
            practitioner = Practitioner.objects.filter(pk=options["practitioner"]).first()
            if practitioner is None:
                raise CommandError(f"Profissional #{options['practitioner']} não existe.")
            return practitioner

        matches = list(
            Practitioner.objects.filter(
                clinic=clinic, name__icontains=options["practitioner_name"]
            )[:10]
        )
        if not matches:
            raise CommandError(
                f"Nenhum profissional de '{clinic.slug}' com "
                f"'{options['practitioner_name']}' no nome."
            )
        if len(matches) > 1:
            found = "; ".join(f"#{p.pk} {p.name}" for p in matches)
            raise CommandError(f"Nome ambíguo - use --practitioner. Achei: {found}")
        return matches[0]


def _flatten(exc: ValidationError) -> list[str]:
    if hasattr(exc, "message_dict"):
        return [msg for messages in exc.message_dict.values() for msg in messages]
    return list(exc.messages)
