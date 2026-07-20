import pytest
from rest_framework.test import APIClient

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership, User
from apps.tenants.models import Clinic

PASSWORD = "medessence-teste-123"


@pytest.fixture
def api_client():
    return APIClient()


@pytest.fixture
def clinic_a(db):
    return Clinic.objects.create(name="Clínica Alfa", slug="clinica-alfa")


@pytest.fixture
def clinic_b(db):
    return Clinic.objects.create(name="Clínica Beta", slug="clinica-beta")


def make_user(email: str, **extra) -> User:
    return User.objects.create_user(
        email=email,
        password=PASSWORD,
        first_name="Teste",
        last_name="MedEssence",
        **extra,
    )


@pytest.fixture
def manager_two_clinics(db, clinic_a, clinic_b):
    """Gestor com vínculo ativo nas duas clínicas - exige X-Clinic-Id."""
    user = make_user("gestor@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.MANAGER)
    Membership.objects.create(user=user, clinic=clinic_b, role=MembershipRole.MANAGER)
    return user


@pytest.fixture
def manager_single_clinic(db, clinic_a):
    """Gestor com um único vínculo - contexto auto-resolvido."""
    user = make_user("gestor.unico@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.MANAGER)
    return user


@pytest.fixture
def attendant_a(db, clinic_a):
    user = make_user("atendente@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.ATTENDANT)
    return user
