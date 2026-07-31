"""
Área de Parceiros (RF-PAR, §4.11) - a agenda dos atendimentos realizados.

Dois invariantes mandam aqui. **A tela não toca no EHR para desenhar um dia**:
o desenho anterior perguntava à vSaúde paciente por paciente quem tinha
receita, 88 chamadas para um dia cheio. E **a cerca do parceiro é por action**:
ele abre a ficha de UM paciente sem ganhar a listagem de todos.
"""

from datetime import datetime, timezone as dt_timezone

import pytest

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership
from apps.core.models import AuditLog
from apps.core.models.audit_log import AuditAction
from apps.patients.models import ClinicalEntry, ClinicalEntryKind, ClinicalOrigin, Patient
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import Appointment, Practitioner
from conftest import make_user

DIA = "/api/v1/partners/day/"
CAL = "/api/v1/partners/calendar/"


def _quando(dia, hora, minuto=0):
    return datetime(2026, 7, dia, hora, minuto, tzinfo=dt_timezone.utc)


@pytest.fixture
def partner_a(db, clinic_a):
    user = make_user("parceiro@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.PARTNER)
    return user


@pytest.fixture
def cenario(db, clinic_a):
    """
    Um dia de clínica: 2 atendidos (um deles DUAS vezes), 1 que faltou e 1
    que só estava agendado. Só os realizados entram.
    """
    medico = Practitioner.objects.create(clinic=clinic_a, name="Dra. Alana Camargo")
    ana = Patient.objects.create(clinic=clinic_a, name="Ana Atendida", external_id="g-ana")
    beto = Patient.objects.create(clinic=clinic_a, name="Beto Atendido", external_id="g-beto")
    faltou = Patient.objects.create(clinic=clinic_a, name="Caio Faltou", external_id="g-caio")
    marcado = Patient.objects.create(clinic=clinic_a, name="Dina Marcada", external_id="g-dina")

    def consulta(paciente, hora, status):
        return Appointment.objects.create(
            clinic=clinic_a,
            patient=paciente,
            practitioner=medico,
            starts_at=_quando(30, hora),
            status=status,
        )

    consulta(ana, 10, AppointmentStatus.COMPLETED)
    consulta(ana, 16, AppointmentStatus.COMPLETED)  # o mesmo paciente, 2x
    consulta(beto, 11, AppointmentStatus.COMPLETED)
    consulta(faltou, 14, AppointmentStatus.NO_SHOW)
    consulta(marcado, 15, AppointmentStatus.SCHEDULED)

    # Um realizado em OUTRO dia, para provar o recorte.
    Appointment.objects.create(
        clinic=clinic_a,
        patient=marcado,
        practitioner=medico,
        starts_at=_quando(22, 9),
        status=AppointmentStatus.COMPLETED,
    )
    return {"medico": medico, "ana": ana, "beto": beto, "faltou": faltou, "marcado": marcado}


# ------------------------------- o dia -------------------------------


def test_o_dia_traz_so_os_atendimentos_REALIZADOS(
    api_client, manager_single_clinic, cenario
):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(DIA, {"date": "2026-07-30"})

    assert resposta.status_code == 200
    nomes = [p["name"] for p in resposta.data["patients"]]
    assert nomes == ["Ana Atendida", "Beto Atendido"]
    assert "Caio Faltou" not in nomes, "faltou não é atendimento realizado"
    assert "Dina Marcada" not in nomes, "agendado não é realizado"


def test_contador_conta_ATENDIMENTOS_e_a_lista_conta_PESSOAS(
    api_client, manager_single_clinic, cenario
):
    """A Ana foi atendida duas vezes: uma linha, dois atendimentos."""
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(DIA, {"date": "2026-07-30"})

    assert resposta.data["kpis"] == {"attendances": 3, "patients": 2}
    assert len(resposta.data["patients"]) == 2


def test_a_linha_e_a_da_listagem_de_pacientes(
    api_client, manager_single_clinic, cenario
):
    api_client.force_authenticate(manager_single_clinic)
    linha = api_client.get(DIA, {"date": "2026-07-30"}).data["patients"][0]

    # Os campos da listagem (RF-PAC-1), não os do atendimento.
    for campo in ("id", "name", "status", "phone", "city", "tags", "last_appointment_at"):
        assert campo in linha, campo


def test_a_tela_NAO_toca_no_EHR_para_desenhar_o_dia(
    api_client, manager_single_clinic, clinic_a, cenario, monkeypatch
):
    """O invariante da fatia: o desenho anterior gastava 2 chamadas por
    paciente, e um dia cheio custava 88."""
    clinic_a.ehr_provider = "fake"
    clinic_a.save(update_fields=["ehr_provider"])

    def explode(*args, **kwargs):
        raise AssertionError("a lista do dia não pode chamar o EHR")

    monkeypatch.setattr("apps.integrations.ehr.registry.get_ehr_provider", explode)
    api_client.force_authenticate(manager_single_clinic)

    assert api_client.get(DIA, {"date": "2026-07-30"}).status_code == 200


def test_filtro_por_profissional(api_client, manager_single_clinic, clinic_a, cenario):
    outro = Practitioner.objects.create(clinic=clinic_a, name="Dr. Bruno")
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(DIA, {"date": "2026-07-30", "practitioner": outro.pk})
    assert resposta.data["kpis"]["attendances"] == 0
    assert resposta.data["patients"] == []


def test_data_invalida_explica(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    assert api_client.get(DIA, {"date": "30/07"}).status_code == 400
    assert api_client.get(DIA).status_code == 400


def test_dia_de_outra_clinica_nao_vaza(api_client, clinic_b, cenario):
    intruso = make_user("gestor.b@teste.dev")
    Membership.objects.create(user=intruso, clinic=clinic_b, role=MembershipRole.MANAGER)
    api_client.force_authenticate(intruso)

    resposta = api_client.get(DIA, {"date": "2026-07-30"})
    assert resposta.data["patients"] == []


# ---------------------------- o calendário ----------------------------


def test_calendario_conta_atendimentos_realizados_por_dia(
    api_client, manager_single_clinic, cenario
):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(CAL, {"year": 2026, "month": 7})

    # Dia 30: 3 realizados (a Ana conta 2). Dia 22: 1. O faltou e o agendado
    # não entram.
    assert resposta.data["by_day"] == {"22": 1, "30": 3}


def test_calendario_de_dezembro_nao_estoura_o_ano(
    api_client, manager_single_clinic, clinic_a, cenario
):
    Appointment.objects.create(
        clinic=clinic_a,
        patient=cenario["ana"],
        practitioner=cenario["medico"],
        starts_at=datetime(2026, 12, 20, 10, tzinfo=dt_timezone.utc),
        status=AppointmentStatus.COMPLETED,
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(CAL, {"year": 2026, "month": 12})

    assert resposta.status_code == 200
    assert resposta.data["by_day"] == {"20": 1}


def test_calendario_recusa_mes_invalido(api_client, manager_single_clinic):
    api_client.force_authenticate(manager_single_clinic)
    assert api_client.get(CAL, {"year": 2026, "month": 13}).status_code == 400
    assert api_client.get(CAL, {"year": "x", "month": 7}).status_code == 400


# ------------------------------- a cerca -------------------------------


def test_parceiro_tem_a_area_e_o_que_a_ficha_precisa(api_client, partner_a, cenario):
    api_client.force_authenticate(partner_a)

    assert api_client.get(DIA, {"date": "2026-07-30"}).status_code == 200
    assert api_client.get(CAL, {"year": 2026, "month": 7}).status_code == 200
    # A ficha de UM paciente e a linha do tempo clínica dele.
    assert api_client.get(f"/api/v1/patients/{cenario['ana'].pk}/").status_code == 200
    assert (
        api_client.get(
            "/api/v1/clinical-entries/", {"patient": cenario["ana"].pk}
        ).status_code
        == 200
    )
    # O catálogo de profissionais, que alimenta o filtro.
    assert api_client.get("/api/v1/practitioners/").status_code == 200
    # E o próprio rastro.
    assert api_client.get("/api/v1/core/my-access/").status_code == 200


def test_parceiro_NAO_tem_a_listagem_de_pacientes(api_client, partner_a, cenario):
    """A cerca é por ACTION: `retrieve` sim, `list` não. Sem isso, abrir a
    ficha teria custado dar a carteira inteira a um usuário externo."""
    api_client.force_authenticate(partner_a)

    assert api_client.get("/api/v1/patients/").status_code == 403
    assert api_client.get(f"/api/v1/patients/{cenario['ana'].pk}/").status_code == 200


def test_parceiro_nao_escreve_registro_clinico(api_client, partner_a, cenario):
    api_client.force_authenticate(partner_a)
    resposta = api_client.post(
        "/api/v1/clinical-entries/",
        {"patient": cenario["ana"].pk, "kind": "note", "date": "2026-07-30T10:00:00Z"},
        format="json",
    )
    assert resposta.status_code == 403


def test_parceiro_continua_fora_do_resto_da_api(api_client, partner_a, cenario):
    api_client.force_authenticate(partner_a)

    assert api_client.get("/api/v1/appointments/").status_code == 403
    assert api_client.get("/api/v1/conversations/").status_code == 403
    assert api_client.get("/api/v1/notifications/").status_code == 403
    assert api_client.get("/api/v1/tags/").status_code == 403


def test_medico_e_atendente_nao_tem_a_area(api_client, attendant_a, cenario):
    api_client.force_authenticate(attendant_a)
    assert api_client.get(DIA, {"date": "2026-07-30"}).status_code == 403


# --------------------- o clínico que o parceiro lê ---------------------


@pytest.fixture
def prontuario(db, clinic_a, cenario):
    def entrada(kind, hora, **extra):
        return ClinicalEntry.objects.create(
            clinic=clinic_a,
            patient=cenario["ana"],
            kind=kind,
            origin=ClinicalOrigin.EHR,
            date=_quando(30, hora),
            **extra,
        )

    entrada(
        ClinicalEntryKind.PRESCRIPTION,
        10,
        document_url="https://app.vsaude.invalido/Export?id=guid-receita",
    )
    entrada(ClinicalEntryKind.EXAM, 11, description="<p>SOLICITO</p>")
    entrada(ClinicalEntryKind.NOTE, 12, text="<p>segredo da consulta</p>")
    entrada(ClinicalEntryKind.FORM_RESPONSE, 13, title="Anamnese")
    return True


def test_parceiro_le_SO_receita_e_exame(api_client, partner_a, cenario, prontuario):
    """O recorte é do SERVIDOR: esconder nota só na tela deixaria o dado a um
    query param de distância."""
    api_client.force_authenticate(partner_a)

    resposta = api_client.get(
        "/api/v1/clinical-entries/", {"patient": cenario["ana"].pk}
    )

    tipos = {e["kind"] for e in resposta.data["results"]}
    assert tipos == {"prescription", "exam"}
    assert "segredo da consulta" not in str(resposta.data)
    assert "Anamnese" not in str(resposta.data)


def test_gestor_continua_vendo_o_prontuario_inteiro(
    api_client, manager_single_clinic, prontuario
):
    api_client.force_authenticate(manager_single_clinic)
    tipos = {
        e["kind"] for e in api_client.get("/api/v1/clinical-entries/").data["results"]
    }
    assert tipos == {"prescription", "exam", "note", "form_response"}


def test_parceiro_ve_CPF_e_contato_e_isso_fica_auditado(
    api_client, partner_a, cenario
):
    """Decisão do usuário em 31/07/2026, ciente de que o parceiro é externo.
    O rastro é o que torna a decisão reversível: dá para saber quem viu."""
    cenario["ana"].cpf = "12345678909"
    cenario["ana"].phone = "5589994068036"
    cenario["ana"].save(update_fields=["cpf", "phone"])
    api_client.force_authenticate(partner_a)

    resposta = api_client.get(f"/api/v1/patients/{cenario['ana'].pk}/")

    assert resposta.data["cpf"] == "12345678909", "CPF completo, não mascarado"
    assert resposta.data["phone"] == "5589994068036"
    assert AuditLog.objects.filter(action=AuditAction.READ_CPF).exists()


# ---------------------------- abrir o PDF ----------------------------


@pytest.fixture
def com_ehr(clinic_a):
    clinic_a.ehr_provider = "fake"
    clinic_a.save(update_fields=["ehr_provider"])
    return clinic_a


def _url_abrir(entrada):
    return f"/api/v1/partners/documents/{entrada.pk}/open/"


def test_abrir_entrega_o_pdf_e_audita(api_client, partner_a, prontuario, com_ehr):
    entrada = ClinicalEntry.objects.filter(kind=ClinicalEntryKind.PRESCRIPTION).first()
    api_client.force_authenticate(partner_a)

    resposta = api_client.get(_url_abrir(entrada))

    assert resposta.status_code == 200
    assert resposta["Content-Type"] == "application/pdf"
    assert resposta.content.startswith(b"%PDF")

    log = AuditLog.objects.filter(
        resource="ClinicalDocument", action=AuditAction.READ
    ).first()
    assert log is not None
    assert log.payload["role"] == "partner"


def test_abrir_NOTA_pelo_id_e_recusado(api_client, partner_a, prontuario, com_ehr):
    """A nota não é assunto da área nem pelo caminho do documento."""
    nota = ClinicalEntry.objects.filter(kind=ClinicalEntryKind.NOTE).first()
    api_client.force_authenticate(partner_a)
    assert api_client.get(_url_abrir(nota)).status_code == 404


def test_abrir_sem_documento_explica(api_client, partner_a, prontuario, com_ehr):
    exame = ClinicalEntry.objects.filter(kind=ClinicalEntryKind.EXAM).first()
    api_client.force_authenticate(partner_a)
    resposta = api_client.get(_url_abrir(exame))
    assert resposta.status_code == 400
    assert "não tem documento" in str(resposta.data)


def test_abrir_nao_vaza_entre_clinicas(api_client, clinic_b, prontuario, com_ehr):
    entrada = ClinicalEntry.objects.filter(kind=ClinicalEntryKind.PRESCRIPTION).first()
    intruso = make_user("gestor.b2@teste.dev")
    Membership.objects.create(user=intruso, clinic=clinic_b, role=MembershipRole.MANAGER)
    api_client.force_authenticate(intruso)
    assert api_client.get(_url_abrir(entrada)).status_code == 404


# ----------------------- vazamento pela API (o escopo) -----------------------
#
# A cerca de PERMISSÃO diz quais views o parceiro abre; o ESCOPO diz sobre
# quem. Faltando a segunda, a tela mostrava 8 pacientes e a API entregava a
# clínica inteira - foi assim que estes três buracos apareceram.


@pytest.fixture
def fora_do_escopo(db, clinic_a, cenario):
    """Paciente que NUNCA foi atendido: a tela do parceiro nunca o mostra."""
    alheio = Patient.objects.create(
        clinic=clinic_a, name="Nunca Atendido", external_id="g-nunca", cpf="98765432100"
    )
    ClinicalEntry.objects.create(
        clinic=clinic_a,
        patient=alheio,
        kind=ClinicalEntryKind.PRESCRIPTION,
        origin=ClinicalOrigin.EHR,
        date=_quando(30, 9),
        document_url="https://app.vsaude.invalido/Export?id=guid-alheio",
    )
    return alheio


def test_parceiro_nao_abre_ficha_de_quem_nunca_foi_atendido(
    api_client, partner_a, fora_do_escopo
):
    """Trocar o id na URL entregava CPF e endereço de qualquer paciente."""
    api_client.force_authenticate(partner_a)
    resposta = api_client.get(f"/api/v1/patients/{fora_do_escopo.pk}/")
    assert resposta.status_code == 404
    assert "98765432100" not in str(resposta.data)


def test_parceiro_nao_lista_o_prontuario_da_clinica_inteira(
    api_client, partner_a, prontuario
):
    """Sem `patient`, a listagem entregava TODO o clínico da clínica."""
    api_client.force_authenticate(partner_a)
    resposta = api_client.get("/api/v1/clinical-entries/")
    assert resposta.status_code == 400
    assert "Informe o paciente" in str(resposta.data)


def test_parceiro_nao_le_o_clinico_de_quem_esta_fora_do_escopo(
    api_client, partner_a, fora_do_escopo
):
    api_client.force_authenticate(partner_a)
    resposta = api_client.get(
        "/api/v1/clinical-entries/", {"patient": fora_do_escopo.pk}
    )
    assert resposta.status_code == 200
    assert resposta.data["results"] == []


def test_parceiro_nao_abre_documento_de_quem_esta_fora_do_escopo(
    api_client, partner_a, fora_do_escopo, com_ehr
):
    entrada = ClinicalEntry.objects.filter(patient=fora_do_escopo).first()
    api_client.force_authenticate(partner_a)
    assert api_client.get(_url_abrir(entrada)).status_code == 404


def test_parceiro_nao_manda_sincronizar_paciente_fora_do_escopo(
    api_client, partner_a, fora_do_escopo, com_ehr
):
    """Senão ele mandava o servidor buscar no EHR o prontuário de qualquer um."""
    api_client.force_authenticate(partner_a)
    resposta = api_client.post(
        "/api/v1/clinical-entries/sync/",
        {"patient": fora_do_escopo.pk},
        format="json",
    )
    assert resposta.status_code == 400


def test_o_gestor_nao_perdeu_nada_com_a_cerca(
    api_client, manager_single_clinic, fora_do_escopo, prontuario
):
    """O escopo é SÓ do parceiro: a clínica continua vendo a clínica."""
    api_client.force_authenticate(manager_single_clinic)

    assert api_client.get(f"/api/v1/patients/{fora_do_escopo.pk}/").status_code == 200
    listagem = api_client.get("/api/v1/clinical-entries/")
    assert listagem.status_code == 200, "gestor não precisa informar paciente"
    assert listagem.data["count"] >= 5
