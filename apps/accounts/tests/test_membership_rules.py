"""
Regras do vínculo médico↔profissional (M3). Existem porque um vínculo
cross-clinic passou pelo admin em 21/07/2026 (trocar a clínica do vínculo
levava o profissional antigo junto) e a carteira do médico vinha VAZIA na
API - `?practitioner=` de outra clínica não resolve.
"""

import pytest
from django.core.exceptions import ValidationError

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership
from apps.scheduling.models import Practitioner
from conftest import make_user


@pytest.fixture
def practitioner_a(db, clinic_a):
    return Practitioner.objects.create(clinic=clinic_a, name="Dra. Alfa")


@pytest.fixture
def practitioner_b(db, clinic_b):
    return Practitioner.objects.create(clinic=clinic_b, name="Dr. Beta")


def test_profissional_de_outra_clinica_e_recusado(db, clinic_a, practitioner_b):
    membership = Membership(
        user=make_user("medico.cross@teste.dev"),
        clinic=clinic_a,
        role=MembershipRole.DOCTOR,
        practitioner=practitioner_b,  # profissional da clínica B
    )
    with pytest.raises(ValidationError) as exc:
        membership.full_clean()
    assert "outra clínica" in str(exc.value)


def test_profissional_da_mesma_clinica_passa(db, clinic_a, practitioner_a):
    membership = Membership(
        user=make_user("medico.ok@teste.dev"),
        clinic=clinic_a,
        role=MembershipRole.DOCTOR,
        practitioner=practitioner_a,
    )
    membership.full_clean()  # não levanta
    membership.save()
    assert membership.pk


def test_profissional_so_em_papel_de_medico(db, clinic_a, practitioner_a):
    membership = Membership(
        user=make_user("gestor.com.carteira@teste.dev"),
        clinic=clinic_a,
        role=MembershipRole.MANAGER,
        practitioner=practitioner_a,
    )
    with pytest.raises(ValidationError) as exc:
        membership.full_clean()
    assert "Médico" in str(exc.value)


def test_carteira_nao_e_compartilhada_entre_dois_medicos(db, clinic_a, practitioner_a):
    Membership.objects.create(
        user=make_user("medico.dono@teste.dev"),
        clinic=clinic_a,
        role=MembershipRole.DOCTOR,
        practitioner=practitioner_a,
    )
    segundo = Membership(
        user=make_user("medico.invasor@teste.dev"),
        clinic=clinic_a,
        role=MembershipRole.DOCTOR,
        practitioner=practitioner_a,
    )
    with pytest.raises(ValidationError) as exc:
        segundo.full_clean()
    assert "já é a carteira" in str(exc.value)


def test_vinculo_sem_profissional_continua_valido(db, clinic_a):
    """Médico ainda não vinculado a uma carteira é estado legítimo (o front
    bloqueia as telas de carteira e pede o vínculo)."""
    membership = Membership(
        user=make_user("medico.sem.carteira@teste.dev"),
        clinic=clinic_a,
        role=MembershipRole.DOCTOR,
    )
    membership.full_clean()
    membership.save()
    assert membership.practitioner_id is None
