"""
O GET /sync/ehr/ que a seção Prontuário lê (RF-CFG-1.6).

O endpoint existia para o front acompanhar a sincronização e nunca teve teste
de leitura; ganhou um quando a tela passou a mostrar as execuções por tipo e
a trava de escrita.
"""

import pytest

from apps.accounts.choices import MembershipRole
from apps.accounts.models.membership import Membership
from conftest import make_user

URL = "/api/v1/sync/ehr/"

pytestmark = pytest.mark.django_db


@pytest.fixture
def gestor_da_a(api_client, clinic_a):
    user = make_user("gestor.sync@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.MANAGER)
    api_client.force_authenticate(user)
    return api_client


def test_estado_traz_a_trava_de_escrita_como_leitura(gestor_da_a, clinic_a):
    """
    RF-CFG-1.6: a fase da clínica (só leitura x escrevendo no EHR) é decisão
    de comando, mas o gestor precisa VER em que fase está sem abrir chamado.
    """
    resposta = gestor_da_a.get(URL)

    assert resposta.status_code == 200
    assert resposta.data["ehr_configured"] is False
    assert resposta.data["ehr_push_enabled"] is False
    assert resposta.data["runs"] != []


def test_a_trava_ligada_aparece_ligada(gestor_da_a, clinic_a):
    clinic_a.ehr_push_enabled = True
    clinic_a.save(update_fields=["ehr_push_enabled"])

    resposta = gestor_da_a.get(URL)

    assert resposta.data["ehr_push_enabled"] is True
