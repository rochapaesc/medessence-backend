"""
O `automation_wipe` (a limpeza total pedida em 18/08).

O que estes testes protegem: ele apaga TUDO de fluxo e sequência (inclusive o
soft-deletado, que foi o que deixou 26 carcaças na clínica 1), não toca no
resto, e não roda sem o `--confirmo`.
"""

from datetime import time

import pytest
from django.core.management import CommandError, call_command

from apps.automation.choices import EnrollmentSource, FlowStatus
from apps.automation.models import Flow, FlowRun, Sequence, SequenceEnrollment, SequenceStep
from apps.automation.sequences import inscrever
from apps.automation.tests.conftest import make_contact, make_conversation, make_flow
from apps.inbox.models import Message
from apps.patients.models import Contact

pytestmark = pytest.mark.django_db


def _cenario(clinic):
    flow = make_flow(clinic, name="Ativo", status=FlowStatus.ACTIVE)
    trilha = Sequence.objects.create(clinic=clinic, name="Trilha", is_active=True)
    SequenceStep.objects.create(
        sequence=trilha, order=1, offset_days=0, send_time=time(9, 0), flow=flow
    )
    contato = make_contact(clinic, wa_id="5585922220001")
    inscrever(trilha, contato, source=EnrollmentSource.PATIENT_RECORD)
    # Uma trilha soft-deletada, que é o que os --limpar dos seeders deixam.
    morta = Sequence.objects.create(clinic=clinic, name="Morta")
    morta.delete()
    return contato


def test_apaga_tudo_inclusive_o_soft_deletado(clinic_a):
    contato = _cenario(clinic_a)

    call_command("automation_wipe", "--clinic", clinic_a.pk, "--confirmo")

    assert Flow.all_objects.count() == 0
    assert Sequence.all_objects.count() == 0
    assert SequenceEnrollment.all_objects.count() == 0
    assert FlowRun.objects.count() == 0
    # O que NÃO é dele: contato fica.
    assert Contact.objects.filter(pk=contato.pk).exists()


def test_sem_confirmo_recusa_e_nada_muda(clinic_a):
    _cenario(clinic_a)

    with pytest.raises(CommandError):
        call_command("automation_wipe", "--clinic", clinic_a.pk)

    assert Sequence.all_objects.count() == 2


def test_nao_vaza_para_outra_clinica(clinic_a, clinic_b):
    _cenario(clinic_a)
    de_fora = make_flow(clinic_b, name="Da outra")

    call_command("automation_wipe", "--clinic", clinic_a.pk, "--confirmo")

    assert Flow.objects.filter(pk=de_fora.pk).exists()
