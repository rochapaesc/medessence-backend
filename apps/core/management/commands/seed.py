"""
Seed de desenvolvimento — IDEMPOTENTE.

Rodar N vezes produz o mesmo estado: os registros são resolvidos por chave
natural (slug, email, nome no catálogo, external_id determinístico) via
get_or_create, e o Faker é semeado para gerar sempre os mesmos nomes.

Uso:
    python manage.py seed
    python manage.py seed --clinics 3 --patients 60
    python manage.py seed --only clinics,users,memberships
    python manage.py seed --only catalogs,patients,appointments

Credenciais criadas (senha padrão: medessence123):
    admin@medessence.dev            → admin da plataforma
    gestor@medessence.dev           → MANAGER em TODAS as clínicas (testa o seletor)
    medico{n}.{slug}@medessence.dev → DOCTOR por clínica (com Practitioner vinculado)
    atendente{n}.{slug}@medessence.dev → ATTENDANT por clínica

Pacientes: distribuição determinística de última consulta para exercitar os
dois status calculados (ativo ≤ 90 dias / inativo) no admin e na API.
"""

from datetime import timedelta

from django.core.management.base import BaseCommand, CommandError
from django.utils import timezone
from faker import Faker

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership, User
from apps.inbox.choices import MessageKind, SenderKind, WhatsAppProviderKind
from apps.inbox.models import (
    Channel,
    Conversation,
    Message,
    QuickReply,
    WhatsAppTemplate,
)
from apps.patients.choices import TagOrigin
from apps.patients.models import Contact, Patient, PatientContact, PatientTag, Tag
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import (
    Appointment,
    CareUnit,
    InsuranceCompany,
    InsurancePlan,
    Practitioner,
    Procedure,
)
from apps.tenants.models import Clinic

SECTIONS = ("clinics", "users", "memberships", "catalogs", "patients", "appointments", "inbox")
DEFAULT_PASSWORD = "medessence123"

CARE_UNITS = ["Unidade Centro", "Unidade Aldeota"]
PROCEDURES = [
    ("Consulta", 30, False),
    ("Retorno", 20, False),
    ("Avaliação", 40, False),
    ("Procedimento estético", 60, False),
    ("Teleconsulta", 30, True),
]
INSURANCES = {"Particular": [], "Unimed": ["Unimed Nacional", "Unimed Regional"]}
TAGS = [
    ("VIP", "#149AA1"),
    ("Pós-operatório", "#EBA01F"),
    ("Convênio", "#3F9CCA"),
    ("Indicação", "#32AE66"),
    ("Inadimplente", "#BC242C"),
    ("Preparo especial", "#276C8E"),
]
CITIES = ["Fortaleza", "Fortaleza", "Fortaleza", "Caucaia", "Maracanaú", "Sobral"]
QUICK_REPLIES = [
    ("Saudação", "Olá! Aqui é da clínica. Como podemos ajudar?"),
    ("Confirmação", "Sua consulta está confirmada. Até breve!"),
    ("Endereço", "Estamos na Av. Central, 1000 — Fortaleza/CE."),
]
WA_TEMPLATES = [
    ("confirmacao_consulta", "UTILITY", "APPROVED"),
    ("lembrete_retorno", "MARKETING", "APPROVED"),
    ("avaliacao_google", "UTILITY", "PENDING"),
]


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
        parser.add_argument("--patients", type=int, default=40, help="Pacientes por clínica.")
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

        if "catalogs" in sections:
            for clinic in clinics:
                self._seed_catalogs(clinic)

        if "patients" in sections:
            for clinic in clinics:
                self._seed_patients(fake, clinic, options["patients"])

        if "appointments" in sections:
            for clinic in clinics:
                self._seed_appointments(clinic)

        if "inbox" in sections:
            for clinic in clinics:
                self._seed_inbox(clinic)

        self.stdout.write(self.style.SUCCESS("Seed concluído."))

    # ------------------------------------------------------------------ #
    # F0 — clínicas, usuários e vínculos
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

    # ------------------------------------------------------------------ #
    # F1 — catálogos, pacientes e agenda
    # ------------------------------------------------------------------ #

    def _seed_catalogs(self, clinic):
        # Profissionais: um por usuário médico da clínica, ligado ao Membership (M3)
        doctor_memberships = clinic.memberships.filter(role=MembershipRole.DOCTOR).select_related(
            "user"
        )
        for membership in doctor_memberships:
            practitioner, created = Practitioner.objects.get_or_create(
                clinic=clinic,
                user=membership.user,
                defaults={"name": f"Dr(a). {membership.user.get_full_name()}"},
            )
            if membership.practitioner_id != practitioner.pk:
                membership.practitioner = practitioner
                membership.save(update_fields=["practitioner", "updated_at"])
            self._log("Practitioner", f"{practitioner.name} @ {clinic.slug}", created)

        for name in CARE_UNITS:
            _, created = CareUnit.objects.get_or_create(clinic=clinic, name=name)
            self._log("CareUnit", f"{name} @ {clinic.slug}", created)

        for name, duration, remotely in PROCEDURES:
            _, created = Procedure.objects.get_or_create(
                clinic=clinic,
                name=name,
                defaults={"duration_min": duration, "remotely": remotely},
            )
            self._log("Procedure", f"{name} @ {clinic.slug}", created)

        for company_name, plans in INSURANCES.items():
            company, created = InsuranceCompany.objects.get_or_create(
                clinic=clinic, name=company_name
            )
            self._log("InsuranceCompany", f"{company_name} @ {clinic.slug}", created)
            for plan_name in plans:
                _, plan_created = InsurancePlan.objects.get_or_create(
                    clinic=clinic, company=company, name=plan_name
                )
                self._log("InsurancePlan", f"{plan_name} @ {clinic.slug}", plan_created)

        for name, color in TAGS:
            _, created = Tag.objects.get_or_create(
                clinic=clinic, name=name, defaults={"color": color}
            )
            self._log("Tag", f"{name} @ {clinic.slug}", created)

    def _seed_patients(self, fake, clinic, quantity):
        tags = list(Tag.objects.filter(clinic=clinic).order_by("name"))
        for index in range(1, quantity + 1):
            email = f"paciente{index}.{clinic.slug}@seed.medessence.dev"
            phone = f"5585{9}{index:08d}"
            patient = Patient.objects.filter(clinic=clinic, email=email).first()
            created = patient is None
            if created:
                patient = Patient.objects.create(
                    clinic=clinic,
                    name=fake.name(),
                    cpf=fake.cpf(),
                    birth_date=fake.date_of_birth(minimum_age=18, maximum_age=85),
                    email=email,
                    phone=phone,
                    city=CITIES[index % len(CITIES)],
                    state="CE",
                    profession=fake.job()[:120],
                )
            else:
                fake.name(), fake.cpf(), fake.date_of_birth(), fake.job()  # mantém a sequência
            self._log("Patient", email, created)

            contact, _ = Contact.objects.get_or_create(
                clinic=clinic,
                wa_id=phone,
                defaults={"display_name": patient.name.split()[0]},
            )
            PatientContact.objects.get_or_create(
                patient=patient,
                contact=contact,
                defaults={"is_primary": True},
            )

            if tags:  # 2 tags determinísticas por paciente
                for offset in (0, 3):
                    tag = tags[(index + offset) % len(tags)]
                    PatientTag.objects.get_or_create(
                        patient=patient,
                        tag=tag,
                        defaults={"origin": TagOrigin.LOCAL},
                    )

    def _seed_appointments(self, clinic):
        """
        Distribuição determinística por paciente (index % 3):
            0 → consulta recente (< 60 dias)      → status ATIVO (janela de 90d)
            1 → consulta antiga (> 8 meses)        → status INATIVO
            2 → sem consulta ou intermediária      → status INATIVO
        Um terço dos pacientes também ganha consulta FUTURA (agenda viva).
        """
        practitioners = list(Practitioner.objects.filter(clinic=clinic).order_by("pk"))
        care_units = list(CareUnit.objects.filter(clinic=clinic).order_by("pk"))
        procedures = list(Procedure.objects.filter(clinic=clinic).order_by("pk"))
        if not practitioners:
            self.stdout.write(
                self.style.WARNING(
                    f"  ! {clinic.slug}: sem profissionais — rode a seção catalogs antes."
                )
            )
            return

        now = timezone.now()
        patients = list(Patient.objects.filter(clinic=clinic).order_by("pk"))
        for index, patient in enumerate(patients, start=1):
            bucket = index % 3
            slots = []
            if bucket == 0:
                slots.append(("past", now - timedelta(days=(index % 55) + 2)))
            elif bucket == 1:
                slots.append(("past", now - timedelta(days=250 + (index % 90))))
            elif index % 6 == 5:  # metade dos "inativos" com consulta intermediária
                slots.append(("past", now - timedelta(days=100 + (index % 60))))

            if index % 3 == 0:
                slots.append(("future", now + timedelta(days=(index % 20) + 1)))

            for kind, starts_at in slots:
                external_id = f"seed-{clinic.slug}-p{index}-{kind}"
                _, created = Appointment.objects.get_or_create(
                    clinic=clinic,
                    external_id=external_id,
                    defaults={
                        "patient": patient,
                        "practitioner": practitioners[index % len(practitioners)],
                        "care_unit": care_units[index % len(care_units)] if care_units else None,
                        "procedure": procedures[index % len(procedures)] if procedures else None,
                        "starts_at": starts_at,
                        "status": (
                            AppointmentStatus.COMPLETED
                            if kind == "past"
                            else AppointmentStatus.SCHEDULED
                        ),
                    },
                )
                if created:
                    self._log("Appointment", external_id, created=True)

    # ------------------------------------------------------------------ #
    # F2 — inbox (canal, conversas, mensagens, respostas rápidas, templates)
    # ------------------------------------------------------------------ #

    def _seed_inbox(self, clinic):
        # Canal FAKE por clínica (dev sem número real) — unicidade por clínica.
        channel, created = Channel.objects.get_or_create(
            clinic=clinic,
            defaults={
                "provider": WhatsAppProviderKind.FAKE,
                "display_number": "5585999990000",
                "phone_number_id": f"fake-pnid-{clinic.slug}",
            },
        )
        self._log("Channel", channel.display_number, created)

        for label, body in QUICK_REPLIES:
            _, qr_created = QuickReply.objects.get_or_create(
                clinic=clinic, label=label, defaults={"body": body}
            )
            self._log("QuickReply", f"{label} @ {clinic.slug}", qr_created)

        for name, category, status in WA_TEMPLATES:
            _, tpl_created = WhatsAppTemplate.objects.get_or_create(
                clinic=clinic,
                name=name,
                language="pt_BR",
                defaults={"category": category, "status": status},
            )
            self._log("WhatsAppTemplate", f"{name} @ {clinic.slug}", tpl_created)

        # Conversas para os primeiros contatos com paciente vinculado.
        now = timezone.now()
        links = list(
            PatientContact.objects.filter(patient__clinic=clinic)
            .select_related("patient", "contact")
            .order_by("pk")[:5]
        )
        for index, link in enumerate(links, start=1):
            conversation, conv_created = Conversation.objects.get_or_create(
                clinic=clinic,
                channel=channel,
                contact=link.contact,
                defaults={"patient": link.patient},
            )
            self._log("Conversation", f"{link.contact} @ {clinic.slug}", conv_created)

            # Uma recebida (abre a janela de 24h) e uma enviada em resposta.
            inbound_at = now - timedelta(hours=index)
            self._seed_message(
                clinic,
                conversation,
                mid=f"seed-{clinic.slug}-c{index}-in",
                sender_kind=SenderKind.CONTACT,
                body="Olá, gostaria de remarcar minha consulta.",
                wa_timestamp=inbound_at,
            )
            self._seed_message(
                clinic,
                conversation,
                mid=f"seed-{clinic.slug}-c{index}-out",
                sender_kind=SenderKind.AGENT,
                body="Claro! Consigo encaixar você esta semana. Qual o melhor dia?",
                wa_timestamp=inbound_at + timedelta(minutes=3),
            )

    def _seed_message(self, clinic, conversation, *, mid, sender_kind, body, wa_timestamp):
        _, created = Message.objects.get_or_create(
            clinic=clinic,
            provider_message_id=mid,
            defaults={
                "conversation": conversation,
                "sender_kind": sender_kind,
                "kind": MessageKind.TEXT,
                "body": body,
                "wa_timestamp": wa_timestamp,
            },
        )
        if created:
            self._log("Message", mid, created=True)

    def _log(self, resource, key, created):
        marker = self.style.SUCCESS("+ criado") if created else self.style.NOTICE("= existente")
        self.stdout.write(f"  {marker}  {resource}: {key}")
