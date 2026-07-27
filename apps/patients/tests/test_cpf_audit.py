"""
Auditoria de quem viu CPF (§15).

O documento só é revelado na FICHA, e cada revelação vira um `READ_CPF` —
a pergunta que uma auditoria de LGPD faz é "quem viu o CPF de quem, e quando",
e ela não deve exigir garimpo no meio dos acessos comuns.
"""

import pytest

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership
from apps.core.models import AuditLog
from apps.core.models.audit_log import AuditAction
from apps.patients.models import Patient
from conftest import make_user

URL = "/api/v1/patients/"
CPF_REAL = "123.456.789-00"
CPF_MASCARADO = "123.***.***-00"


@pytest.fixture
def paciente(db, clinic_a):
    return Patient.objects.create(clinic=clinic_a, name="Paciente CPF", cpf=CPF_REAL)


@pytest.fixture
def doctor_a(db, clinic_a):
    user = make_user("medico.auditoria@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.DOCTOR)
    return user


def _read_cpf_logs(paciente):
    return AuditLog.objects.filter(
        action=AuditAction.READ_CPF, resource="Patient", resource_id=str(paciente.pk)
    )


def test_ficha_do_medico_registra_a_revelacao(api_client, doctor_a, paciente, clinic_a):
    api_client.force_authenticate(doctor_a)

    response = api_client.get(f"{URL}{paciente.pk}/")

    assert response.data["cpf"] == CPF_REAL
    log = _read_cpf_logs(paciente).get()
    assert log.user == doctor_a
    assert log.clinic == clinic_a
    assert log.payload["field"] == "cpf"
    assert log.payload["role"] == MembershipRole.DOCTOR


def test_o_log_nao_guarda_o_documento(api_client, doctor_a, paciente):
    """Auditar o acesso não pode virar um segundo lugar onde o CPF mora."""
    api_client.force_authenticate(doctor_a)

    api_client.get(f"{URL}{paciente.pk}/")

    log = _read_cpf_logs(paciente).get()
    assert "456" not in str(log.payload)
    assert CPF_REAL not in str(log.payload)


def test_atendente_nao_gera_evento(api_client, attendant_a, paciente):
    """Ele recebe mascarado — não houve o que auditar."""
    api_client.force_authenticate(attendant_a)

    response = api_client.get(f"{URL}{paciente.pk}/")

    assert response.data["cpf"] == CPF_MASCARADO
    assert not _read_cpf_logs(paciente).exists()


def test_listagem_nao_revela_nem_audita(
    api_client, manager_single_clinic, paciente
):
    """
    A lista não mostra documento na tela; devolver o CPF de cada linha seria
    expor em massa (e encheria a auditoria de ruído).
    """
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.get(URL)

    (row,) = [p for p in response.data["results"] if p["id"] == paciente.pk]
    assert row["cpf"] == CPF_MASCARADO
    assert CPF_REAL not in str(response.data)
    assert not _read_cpf_logs(paciente).exists()


def test_paciente_sem_cpf_nao_gera_evento(api_client, doctor_a, clinic_a):
    sem_cpf = Patient.objects.create(clinic=clinic_a, name="Sem Documento")
    api_client.force_authenticate(doctor_a)

    api_client.get(f"{URL}{sem_cpf.pk}/")

    assert not _read_cpf_logs(sem_cpf).exists()


def test_cada_acesso_e_um_evento(api_client, doctor_a, paciente):
    """Abrir a ficha duas vezes são dois acessos - a linha do tempo importa."""
    api_client.force_authenticate(doctor_a)

    api_client.get(f"{URL}{paciente.pk}/")
    api_client.get(f"{URL}{paciente.pk}/")

    assert _read_cpf_logs(paciente).count() == 2


def test_gestor_lista_os_eventos_de_cpf(api_client, manager_single_clinic, doctor_a, paciente):
    """O painel de auditoria (só gestor) enxerga o novo tipo."""
    api_client.force_authenticate(doctor_a)
    api_client.get(f"{URL}{paciente.pk}/")

    api_client.force_authenticate(manager_single_clinic)
    response = api_client.get("/api/v1/core/audit-logs/", {"action": AuditAction.READ_CPF})

    assert response.status_code == 200
    acoes = {row["action"] for row in response.data["results"]}
    assert acoes == {AuditAction.READ_CPF}


def test_falha_ao_gravar_o_log_nao_quebra_a_ficha(
    api_client, doctor_a, paciente, monkeypatch
):
    """
    Auditoria é registro, não gatekeeper: se o banco de log falhar, o médico
    ainda abre a ficha. (`log_action` engole a exceção e loga o incidente.)
    """
    from apps.core.models import AuditLog

    def explode(*args, **kwargs):
        raise RuntimeError("log indisponível")

    monkeypatch.setattr(AuditLog.objects, "create", explode)
    api_client.force_authenticate(doctor_a)

    response = api_client.get(f"{URL}{paciente.pk}/")

    assert response.status_code == 200
    assert response.data["cpf"] == CPF_REAL
