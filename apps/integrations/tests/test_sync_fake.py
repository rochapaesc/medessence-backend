"""
Motor de pull ponta a ponta com o provider FAKE - idempotência, diff de
tags (§10.3), vínculo de contatos (RF-PAC-7) e mapeamento de status (P4).
"""

import pytest

from apps.integrations.models import SyncRun
from apps.integrations.services import pull_appointments, pull_catalogs, pull_patients
from apps.patients.choices import TagOrigin, TagSyncScope
from apps.patients.models import Contact, Patient, PatientContact, PatientTag, Tag
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import Appointment, EHRStatusMap, Practitioner
from apps.tenants.choices import EHRProviderKind
from apps.tenants.models import Clinic


@pytest.fixture
def fake_clinic(db):
    """
    Sem montar mapa de status à mão: o do provider FAKE vem da migration
    0009, igual à produção.

    A versão anterior criava o próprio mapa com a semântica ERRADA
    (100→cancelada, 90→completed) e por isso o teste passava enquanto todo
    pull real terminava com `unmapped_statuses` (28/07/2026). Dublê que
    inventa o contrato esconde exatamente o defeito que deveria pegar.
    """
    return Clinic.objects.create(
        name="Clínica Fake", slug="clinica-fake", ehr_provider=EHRProviderKind.FAKE
    )


def test_pull_catalogs_cria_e_e_idempotente(fake_clinic):
    run1 = pull_catalogs(fake_clinic)
    assert run1.stats["tags"]["created"] == 6
    assert run1.stats["procedures"]["created"] == 3
    assert run1.stats["care_units"]["created"] == 2
    assert run1.stats["insurances"]["created"] == 1

    # Tag no limite do bitmask (2^62) armazenada corretamente
    limite = Tag.objects.get(clinic=fake_clinic, name="Limite do bitmask")
    assert int(limite.identifier) == 2**62
    assert limite.sync_scope == TagSyncScope.SYNCED

    run2 = pull_catalogs(fake_clinic)
    assert run2.stats["tags"]["created"] == 0
    assert run2.stats["tags"]["updated"] == 6
    assert Tag.objects.filter(clinic=fake_clinic).count() == 6


def test_tag_local_com_mesmo_nome_e_vinculada_nao_duplicada(fake_clinic):
    local = Tag.objects.create(clinic=fake_clinic, name="VIP", sync_scope=TagSyncScope.LOCAL_ONLY)
    pull_catalogs(fake_clinic)
    local.refresh_from_db()
    assert local.identifier == 1
    assert local.sync_scope == TagSyncScope.SYNCED
    assert Tag.objects.filter(clinic=fake_clinic, name="VIP").count() == 1


def test_pull_patients_upsert_contatos_e_tags(fake_clinic):
    pull_catalogs(fake_clinic)
    run = pull_patients(fake_clinic)
    assert run.stats == {"fetched": 25, "created": 25, "updated": 0, "removed": 0}

    # Telefone compartilhado (pacientes 3 e 4) → um Contact, dois vínculos, UM principal
    shared = Contact.objects.get(clinic=fake_clinic, wa_id__endswith="00003")
    links = PatientContact.objects.filter(contact=shared)
    assert links.count() == 2
    assert links.filter(is_primary=True).count() == 1

    # Paciente sem telefone não gera contato
    sem_fone = Patient.objects.get(clinic=fake_clinic, external_id__endswith="pat-5")
    assert sem_fone.patient_contacts.count() == 0

    # Atribuições espelhadas com origin=EHR (bitmask decodificado)
    assert PatientTag.objects.filter(patient__clinic=fake_clinic, origin=TagOrigin.EHR).exists()

    # Idempotência
    run2 = pull_patients(fake_clinic)
    assert run2.stats == {"fetched": 25, "created": 0, "updated": 25, "removed": 0}
    assert Patient.objects.filter(clinic=fake_clinic).count() == 25


def test_diff_de_tags_preserva_atribuicoes_locais(fake_clinic):
    pull_catalogs(fake_clinic)
    pull_patients(fake_clinic)

    patient = Patient.objects.filter(clinic=fake_clinic).order_by("external_id").first()
    manual = Tag.objects.create(clinic=fake_clinic, name="Manual")
    PatientTag.objects.create(patient=patient, tag=manual, origin=TagOrigin.LOCAL)

    pull_patients(fake_clinic)  # novo pull não pode remover a atribuição local

    assert PatientTag.objects.filter(patient=patient, tag=manual, origin=TagOrigin.LOCAL).exists()


def test_pull_appointments_mapeia_status_e_recalcula_paciente(fake_clinic):
    pull_catalogs(fake_clinic)
    pull_patients(fake_clinic)
    run = pull_appointments(fake_clinic)

    assert run.stats["fetched"] > 0
    assert run.stats["created"] == run.stats["fetched"]
    assert run.stats["unmapped_statuses"] == []

    # Status crus traduzidos com a semântica OFICIAL da vSaúde (P4): 100 é
    # "passou do horário", que o usuário decidiu NÃO contar como
    # comparecimento — não é cancelamento.
    assert Appointment.objects.filter(
        clinic=fake_clinic, source_status="100", status=AppointmentStatus.NO_SHOW
    ).exists()
    assert Appointment.objects.filter(
        clinic=fake_clinic, source_status="51", status=AppointmentStatus.CANCELED
    ).exists()

    # Profissionais upsertados a partir da agenda (sem endpoint próprio)
    assert Practitioner.objects.filter(clinic=fake_clinic).count() == 2

    # Consulta realizada no passado → paciente com last_appointment_at (signal)
    completed = Appointment.objects.filter(
        clinic=fake_clinic, status=AppointmentStatus.COMPLETED
    ).first()
    if completed:
        completed.patient.refresh_from_db()
        assert completed.patient.last_appointment_at is not None

    # Idempotência
    run2 = pull_appointments(fake_clinic)
    assert run2.stats["created"] == 0


def test_pull_nao_regride_consulta_em_atendimento(fake_clinic):
    """Guarda anti-regressão (RF-AGE-5): "Em atendimento" é LOCAL-only - o
    pull não devolve a consulta aos estados pré-atendimento; só avanço entra."""
    pull_catalogs(fake_clinic)
    pull_patients(fake_clinic)
    pull_appointments(fake_clinic)

    appointment = Appointment.objects.filter(
        clinic=fake_clinic, source_status="10"  # mapeia p/ scheduled
    ).first()
    assert appointment is not None
    appointment.status = AppointmentStatus.IN_PROGRESS
    appointment.save(update_fields=["status"])

    pull_appointments(fake_clinic)  # EHR continua reportando "10"
    appointment.refresh_from_db()
    assert appointment.status == AppointmentStatus.IN_PROGRESS  # não regrediu

    # Avanço vindo do EHR entra normalmente (simulado trocando o mapa)
    EHRStatusMap.objects.filter(
        provider=EHRProviderKind.FAKE, source_status="10"
    ).update(status=AppointmentStatus.COMPLETED)
    pull_appointments(fake_clinic)
    appointment.refresh_from_db()
    assert appointment.status == AppointmentStatus.COMPLETED


def test_agenda_busca_paciente_ausente_pontualmente(fake_clinic):
    """Agenda antes do pull de pacientes → refresh pontual via get_patient."""
    pull_catalogs(fake_clinic)
    run = pull_appointments(fake_clinic)
    assert run.stats["patients_fetched"] > 0
    assert Patient.objects.filter(clinic=fake_clinic).count() == run.stats["patients_fetched"]


def test_sync_runs_registrados_com_stats(fake_clinic):
    pull_catalogs(fake_clinic)
    pull_patients(fake_clinic)
    runs = SyncRun.objects.filter(clinic=fake_clinic)
    assert runs.count() == 2
    for run in runs:
        assert run.started_at and run.finished_at
        assert run.stats and not run.error


def test_fake_so_emite_codigo_que_a_vsaude_produz(db):
    """
    Invariante que faltava: TODO código do ciclo do fake precisa existir no
    mapa do fake E no mapa oficial da vSaúde, com o MESMO significado.

    O fake existe para exercitar em dev o caminho que produção percorre. Sem
    esta amarra ele emitia "90" — código que não existe na vSaúde (retirado
    do mapa pela 0008) — e o dev testava um estado impossível.
    """
    from apps.integrations.ehr.fake.adapter import STATUS_CYCLE, FakeAdapter

    # Tudo que o fake pode gravar: o ciclo do pull E os códigos das
    # transições — cobrir só o ciclo deixaria o buraco no caminho de escrita.
    emitidos = [*STATUS_CYCLE, *FakeAdapter.TRANSITION_CODES.values()]

    fake_map = dict(
        EHRStatusMap.objects.filter(provider=EHRProviderKind.FAKE).values_list(
            "source_status", "status"
        )
    )
    vsaude_map = dict(
        EHRStatusMap.objects.filter(provider=EHRProviderKind.VSAUDE).values_list(
            "source_status", "status"
        )
    )

    for code in emitidos:
        assert code in fake_map, f"o fake emite {code} e o mapa dele não cobre"
        assert code in vsaude_map, f"{code} não existe na vSaúde — o fake não deve emiti-lo"
        assert fake_map[code] == vsaude_map[code], (
            f"{code} significa {fake_map[code]} no fake e "
            f"{vsaude_map[code]} na vSaúde — o fake deve espelhar o real"
        )


def test_mapa_do_fake_nao_conhece_o_codigo_90(db):
    """90 ("Em andamento") não existe na vSaúde: nenhum código do EHR produz
    in_progress, que é estado exclusivamente local (RF-AGE-5, D1)."""
    assert not EHRStatusMap.objects.filter(source_status="90").exists()
