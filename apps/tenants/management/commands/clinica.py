"""
Cria (ou atualiza) uma clínica e, de quebra, vincula quem vai cuidar dela.

Existe porque criar tenant era coisa de `seed` ou do admin do Django, e as duas
saídas são ruins: o seed monta o elenco inteiro de dados fake, e o admin não
deixa rastro nenhum de quem criou a clínica nem confere o vínculo.

    manage.py clinica --nome "Clínica Nova" --slug clinica-nova \\
      --gestor gestor@medessence.dev

⚠️ A clínica nasce **sem prontuário e sem canal de WhatsApp**, e é assim de
propósito: os dois são ligados depois, cada um pelo seu caminho (o EHR pelo
admin, o WhatsApp pelo cadastro incorporado da tela, §4.3.3). É esse estado que
a clínica nova de verdade tem no primeiro dia.
"""

from django.core.management.base import BaseCommand, CommandError
from django.db import transaction

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership, User
from apps.core.audit import log_action
from apps.core.models.audit_log import AuditAction
from apps.tenants.models import Clinic


class Command(BaseCommand):
    help = "Cria uma clínica (tenant) e vincula um gestor a ela."

    def add_arguments(self, parser):
        parser.add_argument("--nome", required=True)
        parser.add_argument("--slug", required=True)
        parser.add_argument(
            "--gestor",
            action="append",
            default=[],
            help="E-mail de quem administra. Pode repetir para vincular vários.",
        )
        parser.add_argument(
            "--fuso",
            default="America/Fortaleza",
            help="Fuso da clínica: é ele que decide a hora dos disparos.",
        )

    @transaction.atomic
    def handle(self, *args, **options):
        slug = options["slug"]
        clinica = Clinic.objects.filter(slug=slug).first()
        if clinica is None:
            # `create` dispara o signal que cria o setor Recepção (RF-ATD-5).
            clinica = Clinic.objects.create(
                name=options["nome"],
                slug=slug,
                timezone=options["fuso"],
            )
            # A docstring acima promete rastro de quem criou a clínica, e até
            # 20/08/2026 ele não existia: o comando escrevia no terminal e o
            # AuditLog ficava vazio.
            #
            # ⚠️ Sem usuário de propósito: quem roda isto é o operador no
            # terminal do servidor, não uma conta do produto. O que identifica
            # o evento é a origem no payload.
            log_action(
                user=None,
                action=AuditAction.CREATE,
                resource="Clinic",
                resource_id=clinica.pk,
                payload={
                    "operation": "clinic.create",
                    "slug": slug,
                    "timezone": options["fuso"],
                    "origem": "manage.py clinica",
                },
                clinic=clinica,
            )
            self.stdout.write(
                self.style.SUCCESS(f"Clínica criada: {clinica.name} (#{clinica.pk}, {slug})")
            )
        else:
            self.stdout.write(f"Clínica {slug} já existe (#{clinica.pk}); só vinculando.")

        for email in options["gestor"]:
            self._vincular(clinica, email)

        self.stdout.write(
            "\nEla nasce sem prontuário e sem WhatsApp. Para conectar o número, "
            "entre como gestor e use o convite no Inbox."
        )

    def _vincular(self, clinica, email: str) -> None:
        user = User.objects.filter(email=email).first()
        if user is None:
            raise CommandError(
                f"Usuário '{email}' não existe. Crie-o antes com "
                "`manage.py membership set --user ... --create-user --password ...`."
            )

        vinculo = Membership.objects.filter(user=user, clinic=clinica).first()
        if vinculo is None:
            vinculo = Membership.objects.create(
                user=user, clinic=clinica, role=MembershipRole.MANAGER
            )
            self._auditar_vinculo(clinica, vinculo, AuditAction.CREATE)
            self.stdout.write(self.style.SUCCESS(f"  {email} vinculado como gestor."))
            return

        # Já existia: garante que ele consegue entrar, sem rebaixar quem já é
        # gestor por outro motivo.
        if not vinculo.is_active or vinculo.role != MembershipRole.MANAGER:
            vinculo.is_active = True
            vinculo.role = MembershipRole.MANAGER
            vinculo.save(update_fields=["is_active", "role", "updated_at"])
            # Promover alguém a gestor pelo terminal é o mesmo evento que a
            # tela de equipe registra: ele não pode existir só aqui.
            self._auditar_vinculo(clinica, vinculo, AuditAction.UPDATE)
        self.stdout.write(f"  {email} já tinha vínculo aqui.")

    @staticmethod
    def _auditar_vinculo(clinica, vinculo, action) -> None:
        log_action(
            user=None,
            action=action,
            resource="Membership",
            resource_id=vinculo.pk,
            payload={
                "user": vinculo.user_id,
                "role": vinculo.role,
                "origem": "manage.py clinica",
            },
            clinic=clinica,
        )
