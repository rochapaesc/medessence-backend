"""
Área de Parceiros (RF-PAR, §4.11).

Os dois testes que mais importam: a CERCA do papel partner (um usuário
externo com Membership válido não pode enxergar nada além desta área) e a
conferência do espelho (a tela dispara o pull dos pacientes do período,
porque o prontuário local só sincroniza ao abrir a ficha).
"""

from datetime import datetime, timezone as dt_timezone

import pytest
from django.utils import timezone

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership
from apps.core.models import AuditLog
from apps.core.models.audit_log import AuditAction
from apps.integrations.choices import SyncRunKind
from apps.integrations.models import SyncRun
from apps.patients.models import ClinicalEntry, ClinicalEntryKind, ClinicalOrigin, Patient
from apps.scheduling.models import Appointment, Practitioner
from conftest import make_user

URL = "/api/v1/partners/summary/"


def _quando(dia, hora, minuto=0):
    return datetime(2026, 7, dia, hora, minuto, tzinfo=dt_timezone.utc)


@pytest.fixture
def partner_a(db, clinic_a):
    user = make_user("parceiro@teste.dev")
    Membership.objects.create(user=user, clinic=clinic_a, role=MembershipRole.PARTNER)
    return user


@pytest.fixture
def cenario(db, clinic_a):
    """Um dia de clínica: 2 pacientes com documento, 1 sem, 1 fora do período."""
    medico = Practitioner.objects.create(clinic=clinic_a, name="Dra. Alana Camargo")
    ana = Patient.objects.create(clinic=clinic_a, name="Ana Prescrita", external_id="g-ana")
    beto = Patient.objects.create(clinic=clinic_a, name="Beto Examinado", external_id="g-beto")
    caio = Patient.objects.create(clinic=clinic_a, name="Caio Sem Nada", external_id="g-caio")

    for paciente, hora in ((ana, 10), (beto, 11), (caio, 14)):
        Appointment.objects.create(
            clinic=clinic_a,
            patient=paciente,
            practitioner=medico,
            starts_at=_quando(30, hora),
            status="completed",
        )

    def entrada(paciente, kind, hora, minuto, **extra):
        return ClinicalEntry.objects.create(
            clinic=clinic_a,
            patient=paciente,
            kind=kind,
            origin=ClinicalOrigin.EHR,
            date=_quando(30, hora, minuto),
            practitioner=medico,
            **extra,
        )

    entrada(ana, ClinicalEntryKind.PRESCRIPTION, 10, 22,
            document_url="https://app.vsaude.invalido/Export?id=guid-receita")
    entrada(ana, ClinicalEntryKind.PRESCRIPTION, 10, 24,
            document_url="https://app.vsaude.invalido/Export?id=guid-receita-2")
    entrada(beto, ClinicalEntryKind.EXAM, 11, 40,
            description="<p>SOLICITO</p><p>Eletrocardiograma</p>")
    # Ruídos que NÃO podem aparecer: nota do mesmo dia e receita de outro dia.
    entrada(ana, ClinicalEntryKind.NOTE, 10, 30, text="<p>nota</p>")
    entrada(caio, ClinicalEntryKind.PRESCRIPTION, 9, 0).__class__.objects.filter(
        patient=caio
    ).update(date=_quando(2, 9))

    return {"medico": medico, "ana": ana, "beto": beto, "caio": caio}


@pytest.fixture
def sem_conferencia(monkeypatch):
    """Corta o disparo da conferência - os testes de conteúdo não são sobre ela."""
    chamadas = []
    monkeypatch.setattr(
        "apps.integrations.tasks.sync_partner_records.delay",
        lambda *args: chamadas.append(args),
    )
    return chamadas


# ------------------------------ a cerca ------------------------------


def test_parceiro_NAO_enxerga_o_resto_da_api(api_client, partner_a, cenario):
    """A cerca fail-closed (RF-PAR-6): Membership válido não é passaporte."""
    api_client.force_authenticate(partner_a)

    assert api_client.get("/api/v1/patients/").status_code == 403
    assert api_client.get("/api/v1/appointments/").status_code == 403
    assert api_client.get("/api/v1/conversations/").status_code == 403
    assert api_client.get("/api/v1/clinical-entries/").status_code == 403
    assert api_client.get("/api/v1/notifications/").status_code == 403


def test_parceiro_ve_o_proprio_rastro_em_meus_acessos(api_client, partner_a):
    api_client.force_authenticate(partner_a)
    assert api_client.get("/api/v1/core/my-access/").status_code == 200


def test_medico_e_atendente_nao_tem_a_area(api_client, attendant_a, cenario, sem_conferencia):
    api_client.force_authenticate(attendant_a)
    resposta = api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-30"})
    assert resposta.status_code == 403


def test_parceiro_e_gestor_tem_a_area(
    api_client, partner_a, manager_single_clinic, cenario, sem_conferencia
):
    for usuario in (partner_a, manager_single_clinic):
        api_client.force_authenticate(usuario)
        resposta = api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-30"})
        assert resposta.status_code == 200


# ------------------------------ o resumo ------------------------------


def test_resumo_traz_so_quem_tem_documento_no_periodo(
    api_client, manager_single_clinic, cenario, sem_conferencia
):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-30"})

    dados = resposta.data
    assert dados["kpis"] == {"prescriptions": 2, "exams": 1, "patients": 2}
    nomes = [p["name"] for p in dados["patients"]]
    assert nomes == ["Ana Prescrita", "Beto Examinado"]
    assert "Caio Sem Nada" not in nomes, "sem documento no período, fora da lista"

    ana = dados["patients"][0]
    assert [d["kind"] for d in ana["docs"]] == ["prescription", "prescription"]
    assert ana["appointment"]["practitioner"] == "Dra. Alana Camargo"

    beto = dados["patients"][1]
    # A descrição do exame vira texto puro, sem o HTML do prontuário.
    assert "Eletrocardiograma" in beto["docs"][0]["description"]
    assert "<p>" not in beto["docs"][0]["description"]


def test_nota_do_prontuario_nao_vaza_na_area(
    api_client, manager_single_clinic, cenario, sem_conferencia
):
    """A área é de receita e exame; a NOTA da consulta é conteúdo da ficha."""
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-30"})
    assert "nota" not in str(resposta.data)


def test_filtro_por_profissional(
    api_client, manager_single_clinic, clinic_a, cenario, sem_conferencia
):
    outro = Practitioner.objects.create(clinic=clinic_a, name="Dr. Bruno")
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(
        URL, {"from": "2026-07-30", "to": "2026-07-30", "practitioner": outro.pk}
    )
    assert resposta.data["kpis"]["patients"] == 0


def test_periodo_invalido_explica(api_client, manager_single_clinic, sem_conferencia):
    api_client.force_authenticate(manager_single_clinic)
    assert api_client.get(URL, {"from": "30/07", "to": "2026-07-30"}).status_code == 400
    assert (
        api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-01"}).status_code == 400
    )


# ------------------------- a conferência do espelho -------------------------


def test_abrir_o_periodo_dispara_a_conferencia_dos_atendidos(
    api_client, manager_single_clinic, clinic_a, cenario, sem_conferencia
):
    clinic_a.ehr_provider = "fake"
    clinic_a.save(update_fields=["ehr_provider"])
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-30"})

    assert resposta.data["conference"]["running"] is True
    assert len(sem_conferencia) == 1
    _, pacientes = sem_conferencia[0]
    # TODOS os atendidos do dia entram - inclusive quem ainda não tem
    # documento, que é justamente quem o espelho pode estar devendo.
    assert set(pacientes) == {cenario["ana"].pk, cenario["beto"].pk, cenario["caio"].pk}


def test_conferencia_recente_do_MESMO_publico_nao_dispara_de_novo(
    api_client, manager_single_clinic, clinic_a, cenario, sem_conferencia
):
    clinic_a.ehr_provider = "fake"
    clinic_a.save(update_fields=["ehr_provider"])
    agora = timezone.now()
    SyncRun.objects.create(
        clinic=clinic_a,
        kind=SyncRunKind.MEDICAL_RECORDS,
        started_at=agora,
        finished_at=agora,
        stats={
            "patient_ids": [
                cenario["ana"].pk,
                cenario["beto"].pk,
                cenario["caio"].pk,
            ]
        },
    )
    Patient.objects.filter(clinic=clinic_a).update(clinical_synced_at=agora)
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-30"})

    assert resposta.data["conference"]["running"] is False
    assert sem_conferencia == []


def test_trocar_de_dia_confere_o_publico_NOVO_mesmo_com_rodada_recente(
    api_client, manager_single_clinic, clinic_a, cenario, sem_conferencia
):
    """A trava de 5 minutos é por PÚBLICO, não só por relógio: o calendário
    existe para pular de dia, e o dia novo tem pacientes que a rodada recente
    não cobriu."""
    clinic_a.ehr_provider = "fake"
    clinic_a.save(update_fields=["ehr_provider"])
    agora = timezone.now()
    SyncRun.objects.create(
        clinic=clinic_a,
        kind=SyncRunKind.MEDICAL_RECORDS,
        started_at=agora,
        finished_at=agora,
        # A rodada recente cobriu OUTRO público (o dia 30 de outra tela).
        stats={"patient_ids": [999999]},
    )
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-30"})

    assert resposta.data["conference"]["running"] is True
    assert len(sem_conferencia) == 1


def test_conferencia_em_andamento_avisa_sem_disparar_outra(
    api_client, manager_single_clinic, clinic_a, cenario, sem_conferencia
):
    clinic_a.ehr_provider = "fake"
    clinic_a.save(update_fields=["ehr_provider"])
    SyncRun.objects.create(
        clinic=clinic_a,
        kind=SyncRunKind.MEDICAL_RECORDS,
        started_at=timezone.now(),
        finished_at=None,
    )
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-30"})

    assert resposta.data["conference"]["running"] is True
    assert sem_conferencia == []


def test_clinica_sem_ehr_nao_confere_nada(
    api_client, manager_single_clinic, cenario, sem_conferencia
):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-30"})
    assert resposta.data["conference"]["running"] is False
    assert sem_conferencia == []


# ------------------------------ abrir o PDF ------------------------------


def _url_abrir(entrada):
    return f"/api/v1/partners/documents/{entrada.pk}/open/"


@pytest.fixture
def com_ehr(clinic_a):
    clinic_a.ehr_provider = "fake"
    clinic_a.save(update_fields=["ehr_provider"])
    return clinic_a


def test_abrir_entrega_o_pdf_e_audita(
    api_client, partner_a, cenario, com_ehr, sem_conferencia
):
    entrada = ClinicalEntry.objects.filter(kind=ClinicalEntryKind.PRESCRIPTION).first()
    api_client.force_authenticate(partner_a)

    resposta = api_client.get(_url_abrir(entrada))

    assert resposta.status_code == 200
    assert resposta["Content-Type"] == "application/pdf"
    assert resposta.content.startswith(b"%PDF")
    assert "receita-2026-07-30" in resposta["Content-Disposition"]

    log = AuditLog.objects.filter(
        resource="ClinicalDocument", action=AuditAction.READ
    ).first()
    assert log is not None
    assert log.payload["role"] == "partner"
    assert log.payload["patient"] == entrada.patient_id


def test_abrir_sem_documento_explica(
    api_client, manager_single_clinic, cenario, com_ehr, sem_conferencia
):
    entrada = ClinicalEntry.objects.filter(kind=ClinicalEntryKind.EXAM).first()
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(_url_abrir(entrada))
    assert resposta.status_code == 400
    assert "não tem documento" in str(resposta.data)


def test_abrir_nao_vaza_entre_clinicas(
    api_client, clinic_b, cenario, com_ehr, sem_conferencia
):
    entrada = ClinicalEntry.objects.filter(kind=ClinicalEntryKind.PRESCRIPTION).first()
    intruso = make_user("gestor.b@teste.dev")
    Membership.objects.create(user=intruso, clinic=clinic_b, role=MembershipRole.MANAGER)
    api_client.force_authenticate(intruso)
    assert api_client.get(_url_abrir(entrada)).status_code == 404


def test_atendente_nao_abre_documento(
    api_client, attendant_a, cenario, com_ehr, sem_conferencia
):
    entrada = ClinicalEntry.objects.filter(kind=ClinicalEntryKind.PRESCRIPTION).first()
    api_client.force_authenticate(attendant_a)
    assert api_client.get(_url_abrir(entrada)).status_code == 403


# --------------------- contagem por dia (o calendário) ---------------------


CAL = "/api/v1/partners/calendar/"


def test_calendario_conta_documentos_por_dia(
    api_client, manager_single_clinic, cenario, sem_conferencia
):
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(CAL, {"year": 2026, "month": 7})

    assert resposta.status_code == 200
    # Dia 30: 2 receitas + 1 exame. Dia 2: a receita do Caio.
    assert resposta.data["by_day"] == {"2": 1, "30": 3}
    # A NOTA do dia 30 não entra na conta - a área é de receita e exame.
    assert sum(resposta.data["by_day"].values()) == 4


def test_calendario_respeita_o_filtro_de_profissional(
    api_client, manager_single_clinic, clinic_a, cenario, sem_conferencia
):
    outro = Practitioner.objects.create(clinic=clinic_a, name="Dr. Bruno")
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(
        CAL, {"year": 2026, "month": 7, "practitioner": outro.pk}
    )
    assert resposta.data["by_day"] == {}


def test_calendario_de_dezembro_nao_estoura_o_ano(
    api_client, manager_single_clinic, clinic_a, cenario, sem_conferencia
):
    """Dezembro fecha em 1º de janeiro do ANO seguinte - o mês 13 não existe."""
    ClinicalEntry.objects.create(
        clinic=clinic_a,
        patient=cenario["ana"],
        kind=ClinicalEntryKind.PRESCRIPTION,
        origin=ClinicalOrigin.EHR,
        date=datetime(2026, 12, 20, 10, tzinfo=dt_timezone.utc),
    )
    api_client.force_authenticate(manager_single_clinic)
    resposta = api_client.get(CAL, {"year": 2026, "month": 12})

    assert resposta.status_code == 200
    assert resposta.data["by_day"] == {"20": 1}


def test_calendario_recusa_mes_invalido(
    api_client, manager_single_clinic, sem_conferencia
):
    api_client.force_authenticate(manager_single_clinic)
    assert api_client.get(CAL, {"year": 2026, "month": 13}).status_code == 400
    assert api_client.get(CAL, {"year": "x", "month": 7}).status_code == 400


def test_calendario_tem_a_mesma_cerca_da_area(
    api_client, partner_a, attendant_a, cenario, sem_conferencia
):
    api_client.force_authenticate(partner_a)
    assert api_client.get(CAL, {"year": 2026, "month": 7}).status_code == 200

    api_client.force_authenticate(attendant_a)
    assert api_client.get(CAL, {"year": 2026, "month": 7}).status_code == 403


# ---------------- cobertura honesta e a consulta do dia certo ----------------


def test_a_linha_mostra_a_consulta_DO_DIA_do_documento(
    api_client, manager_single_clinic, clinic_a, cenario, sem_conferencia
):
    """
    Regressão real: indexada só por paciente, a linha do documento do dia 24
    exibia a consulta do dia 14 - 30 documentos de julho saíam assim na
    clínica de verdade.
    """
    ana = cenario["ana"]
    # A Ana ganha uma SEGUNDA consulta, no dia 24, e um documento nesse dia.
    Appointment.objects.create(
        clinic=clinic_a,
        patient=ana,
        practitioner=cenario["medico"],
        starts_at=_quando(24, 9),
        status="completed",
    )
    ClinicalEntry.objects.create(
        clinic=clinic_a,
        patient=ana,
        kind=ClinicalEntryKind.PRESCRIPTION,
        origin=ClinicalOrigin.EHR,
        date=_quando(24, 13),
        practitioner=cenario["medico"],
    )
    api_client.force_authenticate(manager_single_clinic)

    # Só o dia 24: a consulta mostrada é a do 24, não a do 30.
    dia24 = api_client.get(URL, {"from": "2026-07-24", "to": "2026-07-24"})
    linha = dia24.data["patients"][0]
    assert linha["appointment"]["at"].startswith("2026-07-24")

    # O mês inteiro: a Ana tem documento em DOIS dias, então não existe "a
    # consulta" - a linha devolve os dias em vez de escolher uma e mentir.
    mes = api_client.get(URL, {"from": "2026-07-01", "to": "2026-07-31"})
    ana_mes = next(p for p in mes.data["patients"] if p["id"] == ana.pk)
    assert ana_mes["appointment"] is None
    assert ana_mes["days"] == ["2026-07-24", "2026-07-30"]


def test_cobertura_diz_quantos_do_periodo_foram_conferidos(
    api_client, manager_single_clinic, clinic_a, cenario, sem_conferencia
):
    clinic_a.ehr_provider = "fake"
    clinic_a.save(update_fields=["ehr_provider"])
    # Dos 3 do dia, só a Ana já foi conferida.
    Patient.objects.filter(pk=cenario["ana"].pk).update(
        clinical_synced_at=timezone.now()
    )
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-30"})

    conferencia = resposta.data["conference"]
    assert conferencia["checked"] == 1
    assert conferencia["total"] == 3
    assert conferencia["complete"] is False, "não pode dizer que terminou"


def test_periodo_inteiro_conferido_nao_dispara_nem_avisa(
    api_client, manager_single_clinic, clinic_a, cenario, sem_conferencia
):
    clinic_a.ehr_provider = "fake"
    clinic_a.save(update_fields=["ehr_provider"])
    Patient.objects.filter(clinic=clinic_a).update(clinical_synced_at=timezone.now())
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-30"})

    assert resposta.data["conference"] == {
        "running": False,
        "checked": 3,
        "total": 3,
        "complete": True,
    }
    assert sem_conferencia == []


def test_a_conferencia_pega_so_quem_FALTA_e_avanca_a_fila(
    api_client, manager_single_clinic, clinic_a, cenario, sem_conferencia
):
    """Antes ela remirava os mesmos 60 para sempre; agora a fila anda."""
    clinic_a.ehr_provider = "fake"
    clinic_a.save(update_fields=["ehr_provider"])
    Patient.objects.filter(pk=cenario["ana"].pk).update(
        clinical_synced_at=timezone.now()
    )
    api_client.force_authenticate(manager_single_clinic)

    api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-30"})

    _, pedidos = sem_conferencia[0]
    assert cenario["ana"].pk not in pedidos, "já conferida, não repete"
    assert set(pedidos) == {cenario["beto"].pk, cenario["caio"].pk}


def test_paciente_com_documento_SEM_consulta_entra_na_conferencia(
    api_client, manager_single_clinic, clinic_a, cenario, sem_conferencia
):
    """A renovação sem consulta é caso de tela; precisa ser reconferível."""
    clinic_a.ehr_provider = "fake"
    clinic_a.save(update_fields=["ehr_provider"])
    solto = Patient.objects.create(
        clinic=clinic_a, name="Só Renovação", external_id="g-solto"
    )
    ClinicalEntry.objects.create(
        clinic=clinic_a,
        patient=solto,
        kind=ClinicalEntryKind.PRESCRIPTION,
        origin=ClinicalOrigin.EHR,
        date=_quando(30, 16),
    )
    api_client.force_authenticate(manager_single_clinic)

    resposta = api_client.get(URL, {"from": "2026-07-30", "to": "2026-07-30"})

    assert resposta.data["conference"]["total"] == 4
    _, pedidos = sem_conferencia[0]
    assert solto.pk in pedidos
