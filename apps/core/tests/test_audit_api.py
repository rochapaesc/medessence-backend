"""
API da auditoria (§15): a tela do gestor lê daqui.

Cobre o que a tela precisa (usuário legível, nome do paciente, resumo do
período, CSV) e as duas garantias que a fazem confiável: escopo por clínica e
o fato de que consultar a auditoria também deixa rastro.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership
from apps.core.models import AuditLog
from apps.core.models.audit_log import AuditAction
from apps.patients.models import Patient
from conftest import PASSWORD

URL = "/api/v1/core/audit-logs/"


@pytest.fixture
def doctor_a(db, clinic_a):
    from apps.accounts.models import User

    user = User.objects.create_user(
        email="medica.audit@teste.dev",
        password=PASSWORD,
        first_name="Emanuella",
        last_name="Cavalcante",
    )
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.DOCTOR)
    return user


@pytest.fixture
def paciente(db, clinic_a):
    return Patient.objects.create(clinic=clinic_a, name="Abelardo de Sousa", cpf="12345678909")


@pytest.fixture
def eventos(db, clinic_a, doctor_a, paciente):
    """Um de cada tipo que a tela mostra, com idades diferentes."""
    agora = timezone.now()
    feitos = {
        "cpf": AuditLog.objects.create(
            user=doctor_a,
            clinic=clinic_a,
            action=AuditAction.READ_CPF,
            resource="Patient",
            resource_id=str(paciente.pk),
            payload={"field": "cpf", "role": "doctor"},
            ip_address="192.168.65.1",
        ),
        "update": AuditLog.objects.create(
            user=doctor_a,
            clinic=clinic_a,
            action=AuditAction.UPDATE,
            resource="Patient",
            resource_id=str(paciente.pk),
            payload={"changed_fields": ["phone", "city"], "before": {"phone": "111"}},
        ),
        "create": AuditLog.objects.create(
            user=doctor_a,
            clinic=clinic_a,
            action=AuditAction.CREATE,
            resource="Patient",
            resource_id=str(paciente.pk),
        ),
    }
    # Evento antigo, para exercitar o filtro de período.
    antigo = AuditLog.objects.create(
        user=doctor_a,
        clinic=clinic_a,
        action=AuditAction.READ_CPF,
        resource="Patient",
        resource_id=str(paciente.pk),
    )
    AuditLog.objects.filter(pk=antigo.pk).update(timestamp=agora - timedelta(days=90))
    feitos["antigo"] = antigo
    return feitos


def _logado(api_client, user):
    api_client.force_authenticate(user)
    return api_client


# ─────────────────────────── linha da tabela ────────────────────────────


def test_linha_traz_usuario_papel_e_nome_do_paciente(
    api_client, manager_single_clinic, eventos, doctor_a, paciente
):
    """A tela não deve precisar de outra chamada para escrever uma linha."""
    response = _logado(api_client, manager_single_clinic).get(URL)

    linha = next(r for r in response.data["results"] if r["id"] == eventos["cpf"].pk)
    assert linha["user"]["name"] == "Emanuella Cavalcante"
    assert linha["user"]["email"] == doctor_a.email
    assert linha["user"]["role"] == MembershipRole.DOCTOR
    assert linha["resource_label"] == paciente.name
    assert linha["action"] == AuditAction.READ_CPF
    assert linha["action_display"] == "Leitura de CPF"
    assert linha["ip_address"] == "192.168.65.1"


def test_evento_sem_usuario_nao_quebra_a_linha(
    api_client, manager_single_clinic, clinic_a
):
    """Login com e-mail inexistente entra sem user — a linha continua válida."""
    AuditLog.objects.create(
        clinic=clinic_a, action=AuditAction.LOGIN_FAILED, resource="Auth", resource_id=""
    )

    response = _logado(api_client, manager_single_clinic).get(URL)

    linha = next(
        r for r in response.data["results"] if r["action"] == AuditAction.LOGIN_FAILED
    )
    assert linha["user"] is None


def test_detalhe_mostra_campos_alterados_sem_valores(
    api_client, manager_single_clinic, eventos
):
    """Auditoria não pode virar um segundo lugar onde o dado pessoal mora."""
    response = _logado(api_client, manager_single_clinic).get(
        f"{URL}{eventos['update'].pk}/"
    )

    assert response.data["changed_fields"] == ["phone", "city"]
    assert "111" not in str(response.data), "o valor anterior não vai para a tela"


# ─────────────────────────────── filtros ────────────────────────────────


def test_filtra_por_tipo_de_evento(api_client, manager_single_clinic, eventos):
    response = _logado(api_client, manager_single_clinic).get(
        URL, {"action": AuditAction.READ_CPF}
    )

    assert {r["action"] for r in response.data["results"]} == {AuditAction.READ_CPF}


def test_filtra_por_periodo(api_client, manager_single_clinic, eventos):
    desde = (timezone.now() - timedelta(days=7)).date().isoformat()

    response = _logado(api_client, manager_single_clinic).get(
        URL, {"timestamp_after": desde}
    )

    ids = {r["id"] for r in response.data["results"]}
    assert eventos["antigo"].pk not in ids, "o de 90 dias atrás ficou fora"
    assert eventos["cpf"].pk in ids


def test_filtra_por_paciente(api_client, manager_single_clinic, eventos, paciente):
    response = _logado(api_client, manager_single_clinic).get(
        URL, {"resource": "Patient", "resource_id": str(paciente.pk)}
    )

    assert response.data["count"] >= 3


def test_busca_por_usuario(api_client, manager_single_clinic, eventos):
    response = _logado(api_client, manager_single_clinic).get(URL, {"search": "medica.audit"})

    assert response.data["count"] >= 3


# ──────────────────────────────── resumo ────────────────────────────────


def test_resumo_conta_documentos_pessoas_e_pacientes(
    api_client, manager_single_clinic, eventos
):
    response = _logado(api_client, manager_single_clinic).get(f"{URL}summary/")

    documentos = response.data["documents_seen"]
    assert documentos["total"] == 2  # o de hoje + o antigo
    assert documentos["viewers"] == 1
    assert documentos["patients"] == 1
    assert response.data["updates"] == 1
    assert response.data["creates"] == 1


def test_resumo_respeita_o_filtro_de_periodo(
    api_client, manager_single_clinic, eventos
):
    desde = (timezone.now() - timedelta(days=7)).date().isoformat()

    response = _logado(api_client, manager_single_clinic).get(
        f"{URL}summary/", {"timestamp_after": desde}
    )

    assert response.data["documents_seen"]["total"] == 1


# ───────────────────────────────── CSV ──────────────────────────────────


def test_export_traz_as_colunas_legiveis(api_client, manager_single_clinic, eventos, paciente):
    response = _logado(api_client, manager_single_clinic).get(f"{URL}export/")

    assert response.status_code == 200
    assert response["Content-Type"].startswith("text/csv")
    assert "attachment;" in response["Content-Disposition"]

    corpo = response.content.decode("utf-8")
    assert "Data e hora;Usuário;E-mail;Papel;Evento" in corpo
    assert "Emanuella Cavalcante" in corpo
    assert paciente.name in corpo
    assert "Leitura de CPF" in corpo


def test_export_respeita_os_filtros(api_client, manager_single_clinic, eventos):
    response = _logado(api_client, manager_single_clinic).get(
        f"{URL}export/", {"action": AuditAction.CREATE}
    )

    corpo = response.content.decode("utf-8")
    assert "Criação" in corpo
    assert "Leitura de CPF" not in corpo


# ───────────────────── escopo e rastro da consulta ──────────────────────


def test_nao_enxerga_a_auditoria_de_outra_clinica(
    api_client, manager_two_clinics, clinic_a, clinic_b, eventos
):
    AuditLog.objects.create(
        clinic=clinic_b, action=AuditAction.CREATE, resource="Patient", resource_id="99"
    )

    api_client.force_authenticate(manager_two_clinics)
    response = api_client.get(URL, headers={"X-Clinic-Id": str(clinic_a.pk)})

    assert all(r["resource_id"] != "99" for r in response.data["results"])


def test_consultar_a_auditoria_deixa_rastro(api_client, manager_single_clinic, eventos):
    """Sem isto, o acesso do gestor seria o único ponto cego do sistema."""
    _logado(api_client, manager_single_clinic).get(URL, {"action": AuditAction.READ_CPF})

    rastro = AuditLog.objects.filter(resource="AuditLog", action=AuditAction.READ).get()
    assert rastro.user == manager_single_clinic
    assert rastro.payload["view"] == "list"
    assert rastro.payload["filters"]["action"] == AuditAction.READ_CPF


def test_export_registra_quantas_linhas_sairam(
    api_client, manager_single_clinic, eventos
):
    _logado(api_client, manager_single_clinic).get(f"{URL}export/")

    rastro = AuditLog.objects.filter(
        resource="AuditLog", payload__view="export"
    ).get()
    assert rastro.payload["rows"] == 4


def test_medico_nao_acessa_o_resumo_nem_o_csv(api_client, doctor_a, eventos):
    """A tela é do gestor — as ações novas seguem a mesma permissão."""
    client = _logado(api_client, doctor_a)

    assert client.get(URL).status_code == 403
    assert client.get(f"{URL}summary/").status_code == 403
    assert client.get(f"{URL}export/").status_code == 403
