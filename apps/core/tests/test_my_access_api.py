"""
"Meus acessos" (§15.2): cada usuário vê o histórico do que ELE mesmo fez.

O que estes testes protegem não é o conteúdo da tela — é o recorte. Um
endpoint de auditoria que devolve linha de terceiro deixa de ser transparência
e vira vazamento, então o grosso daqui tenta justamente ampliar o escopo e
falha de propósito.
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership, User
from apps.core.models import AuditLog
from apps.core.models.audit_log import AuditAction
from apps.patients.models import Patient
from conftest import PASSWORD

URL = "/api/v1/core/my-access/"
URL_GESTOR = "/api/v1/core/audit-logs/"


@pytest.fixture
def doctor_a(db, clinic_a):
    user = User.objects.create_user(
        email="medica.meusacessos@teste.dev",
        password=PASSWORD,
        first_name="Emanuella",
        last_name="Cavalcante",
    )
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.DOCTOR)
    return user


@pytest.fixture
def colega(db, clinic_a):
    """Outro médico da MESMA clínica — o vizinho de quem não se pode ver nada."""
    user = User.objects.create_user(
        email="colega.meusacessos@teste.dev",
        password=PASSWORD,
        first_name="Abelardo",
        last_name="Nunes",
    )
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.DOCTOR)
    return user


@pytest.fixture
def paciente(db, clinic_a):
    return Patient.objects.create(
        clinic=clinic_a, name="Marina Duarte", cpf="12345678909"
    )


@pytest.fixture
def eventos(db, clinic_a, doctor_a, colega, paciente):
    """Eventos do médico e um do colega, para o recorte ter o que separar."""
    feitos = {
        "cpf": AuditLog.objects.create(
            user=doctor_a,
            clinic=clinic_a,
            action=AuditAction.READ_CPF,
            resource="Patient",
            resource_id=str(paciente.pk),
            payload={"field": "cpf", "role": "doctor"},
            ip_address="189.45.12.203",
        ),
        "update": AuditLog.objects.create(
            user=doctor_a,
            clinic=clinic_a,
            action=AuditAction.UPDATE,
            resource="Patient",
            resource_id=str(paciente.pk),
            # O valor anterior usa um sentinela, e não um número: "111" bateria
            # por acaso dentro de um id quando as sequências crescerem.
            payload={
                "changed_fields": ["phone", "email"],
                "before": {"phone": "NAO-DEVE-VAZAR"},
            },
        ),
        "do_colega": AuditLog.objects.create(
            user=colega,
            clinic=clinic_a,
            action=AuditAction.READ_CPF,
            resource="Patient",
            resource_id=str(paciente.pk),
        ),
    }
    antigo = AuditLog.objects.create(
        user=doctor_a,
        clinic=clinic_a,
        action=AuditAction.CREATE,
        resource="Patient",
        resource_id=str(paciente.pk),
    )
    AuditLog.objects.filter(pk=antigo.pk).update(
        timestamp=timezone.now() - timedelta(days=90)
    )
    feitos["antigo"] = antigo
    return feitos


def _logado(api_client, user):
    api_client.force_authenticate(user)
    return api_client


# ──────────────────────────────── recorte ────────────────────────────────


def test_ve_so_os_proprios_eventos(api_client, doctor_a, eventos):
    response = _logado(api_client, doctor_a).get(URL)

    ids = {r["id"] for r in response.data["results"]}
    assert eventos["cpf"].pk in ids
    assert eventos["update"].pk in ids
    assert eventos["do_colega"].pk not in ids, "evento de colega não entra"


def test_filtro_de_usuario_do_cliente_nao_amplia_o_recorte(
    api_client, doctor_a, colega, eventos
):
    """
    O recorte é do servidor. Nem o filtro do gestor (?user=) nem qualquer
    variação dele alcançam a linha do colega.
    """
    client = _logado(api_client, doctor_a)

    for params in ({"user": colega.pk}, {"user_email": colega.email}):
        response = client.get(URL, params)
        ids = {r["id"] for r in response.data["results"]}
        assert eventos["do_colega"].pk not in ids, f"{params} não pode ampliar"


def test_linha_nao_tem_campo_de_usuario(api_client, doctor_a, eventos):
    """Sem o campo, não há como devolver terceiro nem por engano."""
    response = _logado(api_client, doctor_a).get(URL)

    linha = response.data["results"][0]
    assert "user" not in linha
    assert linha["resource_label"] == "Marina Duarte"


def test_atendente_e_gestor_tambem_tem_a_propria_lista(
    api_client, attendant_a, manager_single_clinic, clinic_a
):
    """A tela vale para os três papéis — a auditoria do gestor é que é dele."""
    for pessoa in (attendant_a, manager_single_clinic):
        AuditLog.objects.create(
            user=pessoa,
            clinic=clinic_a,
            action=AuditAction.LOGIN,
            resource="Auth",
            resource_id="",
        )

    for pessoa in (attendant_a, manager_single_clinic):
        response = _logado(api_client, pessoa).get(URL)
        assert response.status_code == 200
        assert {r["id"] for r in response.data["results"]} == {
            log.pk for log in AuditLog.objects.filter(user=pessoa)
        }


def test_evento_de_outra_clinica_nao_aparece(api_client, doctor_a, clinic_b):
    """O escopo por clínica continua valendo por cima do recorte por usuário."""
    de_fora = AuditLog.objects.create(
        user=doctor_a,
        clinic=clinic_b,
        action=AuditAction.LOGIN,
        resource="Auth",
        resource_id="",
    )

    response = _logado(api_client, doctor_a).get(URL)

    assert de_fora.pk not in {r["id"] for r in response.data["results"]}


# ─────────────────────────── consultar não deixa rastro ───────────────────


def test_abrir_a_propria_lista_nao_gera_registro(api_client, doctor_a, eventos):
    """
    Ver o próprio log não é acesso a dado de terceiro. Registrar isso geraria
    ruído auto-referente e encheria a auditoria do gestor.
    """
    antes = AuditLog.objects.count()
    client = _logado(api_client, doctor_a)

    client.get(URL)
    client.get(f"{URL}summary/")

    assert AuditLog.objects.count() == antes


def test_a_auditoria_do_gestor_continua_deixando_rastro(
    api_client, manager_single_clinic
):
    """Contraprova: a regra de não registrar é só desta tela."""
    antes = AuditLog.objects.filter(resource="AuditLog").count()

    _logado(api_client, manager_single_clinic).get(URL_GESTOR)

    assert AuditLog.objects.filter(resource="AuditLog").count() == antes + 1


# ──────────────────────────── conteúdo e filtros ──────────────────────────


def test_listagem_ja_traz_os_campos_alterados(api_client, doctor_a, eventos):
    """A linha expande na hora — sem uma segunda chamada por evento."""
    response = _logado(api_client, doctor_a).get(URL)

    linha = next(r for r in response.data["results"] if r["id"] == eventos["update"].pk)
    assert linha["changed_fields"] == ["phone", "email"]
    assert "NAO-DEVE-VAZAR" not in str(response.data), (
        "o valor anterior não vai para a tela"
    )


def test_filtra_por_periodo_e_por_tipo(api_client, doctor_a, eventos):
    client = _logado(api_client, doctor_a)

    recentes = client.get(
        URL, {"timestamp_after": (timezone.now() - timedelta(days=30)).isoformat()}
    )
    assert eventos["antigo"].pk not in {r["id"] for r in recentes.data["results"]}

    so_cpf = client.get(URL, {"action": AuditAction.READ_CPF})
    assert {r["id"] for r in so_cpf.data["results"]} == {eventos["cpf"].pk}


def test_ordena_do_mais_recente_para_o_mais_antigo(api_client, doctor_a, eventos):
    response = _logado(api_client, doctor_a).get(URL)

    datas = [r["timestamp"] for r in response.data["results"]]
    assert datas == sorted(datas, reverse=True)


def test_resumo_conta_so_o_que_e_do_usuario(api_client, doctor_a, eventos):
    response = _logado(api_client, doctor_a).get(f"{URL}summary/")

    assert response.data["total"] == 3, "os 3 do médico, não o do colega"
    assert response.data["documents_seen"] == 1


def test_resumo_respeita_os_filtros_da_lista(api_client, doctor_a, eventos):
    response = _logado(api_client, doctor_a).get(
        f"{URL}summary/", {"action": AuditAction.READ_CPF}
    )

    assert response.data["total"] == 1
    assert response.data["documents_seen"] == 1


# ──────────────────────────────── escrita ─────────────────────────────────


def test_endpoint_e_somente_leitura(api_client, doctor_a, eventos):
    """O log é imutável: nem criar, nem apagar o próprio rastro."""
    client = _logado(api_client, doctor_a)

    assert client.post(URL, {"action": AuditAction.LOGIN}).status_code == 405
    assert client.delete(f"{URL}{eventos['cpf'].pk}/").status_code in (404, 405)
