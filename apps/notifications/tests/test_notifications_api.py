"""
Central de notificações - derivação do feed, marca d'água e escopo por clínica.

Os testes de janela passam `now` explícito para `build_feed`: sem freezegun no
projeto, injetar o instante é o que torna o recorte determinístico (senão o
resultado dependeria da hora em que a suíte roda).
"""

from datetime import timedelta

import pytest
from django.utils import timezone

from apps.integrations.choices import SyncRunKind
from apps.integrations.models import SyncRun
from apps.notifications.choices import NotificationKind
from apps.notifications.models import NotificationRead
from apps.notifications.services import (
    NO_SHOW_LIMIT,
    NO_SHOW_WINDOW,
    PENDING_OUTCOME_GRACE,
    build_feed,
)
from apps.patients.models import Patient
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import Appointment, Practitioner

URL = "/api/v1/notifications/"
COUNTERS_URL = "/api/v1/notifications/counters/"
READ_URL = "/api/v1/notifications/read/"


@pytest.fixture
def crm_a(clinic_a):
    return {
        "patient": Patient.objects.create(clinic=clinic_a, name="Paciente Alfa"),
        "practitioner": Practitioner.objects.create(clinic=clinic_a, name="Dra. Alfa"),
    }


def _appointment(clinic, crm, starts_at, **overrides):
    data = {
        "clinic": clinic,
        "patient": crm["patient"],
        "practitioner": crm["practitioner"],
        "starts_at": starts_at,
        "status": AppointmentStatus.NO_SHOW,
    }
    data.update(overrides)
    return Appointment.objects.create(**data)


def _confirmed(clinic, crm, starts_at, **overrides):
    """Consulta confirmada - o caso comum de "ainda vai acontecer"."""
    return _appointment(clinic, crm, starts_at, status=AppointmentStatus.CONFIRMED, **overrides)


def _patients(clinic, count):
    """Pacientes distintos - o bloco de faltas guarda uma linha por paciente."""
    return [Patient.objects.create(clinic=clinic, name=f"Paciente {i}") for i in range(count)]


def _kinds(feed):
    return [item.kind for item in feed.items]


def _of_kind(feed, kind):
    return [item for item in feed.items if item.kind == kind]


def _local_day_start(moment):
    return timezone.localtime(moment).replace(hour=0, minute=0, second=0, microsecond=0)


# ─────────────────────────── janelas de cada bloco ───────────────────────────


def test_falta_recente_entra_e_falta_antiga_fica_de_fora(clinic_a, crm_a):
    now = timezone.now()
    _appointment(clinic_a, crm_a, now - timedelta(days=2))
    _appointment(clinic_a, crm_a, now - NO_SHOW_WINDOW - timedelta(days=1))

    faltas = _of_kind(build_feed(clinic_a, now=now), NotificationKind.NO_SHOW)

    assert len(faltas) == 1
    # Só o nome no título: o bloco e a pílula já dizem que é falta.
    assert faltas[0].title == "Paciente Alfa"
    assert faltas[0].pill == "Faltou"


def test_consulta_sem_desfecho_respeita_a_carencia(clinic_a, crm_a):
    now = timezone.now()
    # Terminou agora: ainda não é pendência.
    _appointment(
        clinic_a,
        crm_a,
        now - PENDING_OUTCOME_GRACE + timedelta(minutes=5),
        status=AppointmentStatus.CONFIRMED,
    )
    # Passou da carência: vira pendência.
    _appointment(
        clinic_a,
        crm_a,
        now - PENDING_OUTCOME_GRACE - timedelta(hours=1),
        status=AppointmentStatus.SCHEDULED,
    )

    pendencias = _of_kind(build_feed(clinic_a, now=now), NotificationKind.PENDING_OUTCOME)

    assert len(pendencias) == 1
    assert pendencias[0].title == "Paciente Alfa"
    assert pendencias[0].pill == "Ainda “Agendada”"


def test_consulta_ja_realizada_nao_vira_pendencia(clinic_a, crm_a):
    now = timezone.now()
    _appointment(
        clinic_a,
        crm_a,
        now - timedelta(days=1),
        status=AppointmentStatus.COMPLETED,
    )

    feed = build_feed(clinic_a, now=now)

    assert NotificationKind.PENDING_OUTCOME not in _kinds(feed)


def test_hoje_mostra_so_o_que_ainda_vai_acontecer(clinic_a, crm_a):
    # `now` sintético às 8h locais: independe da hora em que a suíte roda.
    day_start = _local_day_start(timezone.now())
    now = day_start + timedelta(hours=8)

    _confirmed(clinic_a, crm_a, day_start + timedelta(hours=12))
    _confirmed(clinic_a, crm_a, day_start + timedelta(hours=3))

    hoje = _of_kind(build_feed(clinic_a, now=now), NotificationKind.APPOINTMENT_TODAY)

    assert len(hoje) == 1
    assert hoje[0].title == "Paciente Alfa"
    # A hora vai na coluna da direita: `occurred_at` aqui é a virada do dia.
    assert hoje[0].when_label == "12:00"


def test_consulta_cancelada_nao_aparece_em_lugar_nenhum(clinic_a, crm_a):
    day_start = _local_day_start(timezone.now())
    now = day_start + timedelta(hours=8)
    _appointment(
        clinic_a,
        crm_a,
        day_start + timedelta(hours=12),
        status=AppointmentStatus.CANCELED,
    )

    assert build_feed(clinic_a, now=now).items == []


def test_falha_de_sync_entra_e_run_bem_sucedido_nao(clinic_a):
    now = timezone.now()
    SyncRun.objects.create(
        clinic=clinic_a,
        kind=SyncRunKind.APPOINTMENTS,
        started_at=now - timedelta(minutes=20),
        finished_at=now - timedelta(minutes=12),
        error="HTTPError 502 em ScheduleService/GetAppointments",
    )
    SyncRun.objects.create(
        clinic=clinic_a,
        kind=SyncRunKind.CATALOGS,
        started_at=now - timedelta(minutes=20),
        finished_at=now - timedelta(minutes=18),
    )

    falhas = _of_kind(build_feed(clinic_a, now=now), NotificationKind.SYNC_FAILED)

    assert len(falhas) == 1
    assert falhas[0].title == "Falha ao sincronizar Agenda"
    assert falhas[0].detail.startswith("HTTPError 502")


def test_sync_que_voltou_a_funcionar_some_do_feed(clinic_a):
    """Só o ÚLTIMO run de cada tipo conta - o feed é verdade no instante da leitura."""
    now = timezone.now()
    SyncRun.objects.create(
        clinic=clinic_a,
        kind=SyncRunKind.APPOINTMENTS,
        started_at=now - timedelta(hours=2),
        finished_at=now - timedelta(hours=2),
        error="caiu",
    )
    SyncRun.objects.create(
        clinic=clinic_a,
        kind=SyncRunKind.APPOINTMENTS,
        started_at=now - timedelta(minutes=5),
        finished_at=now - timedelta(minutes=4),
    )

    assert build_feed(clinic_a, now=now).items == []


def test_teto_corta_a_lista_mas_nao_a_contagem(clinic_a, crm_a):
    """
    Com o bloco fechado o cabeçalho é a única informação na tela — se ele
    mostrasse o tamanho da lista truncada, mentiria sobre o tamanho do problema.
    """
    now = timezone.now()
    for i, patient in enumerate(_patients(clinic_a, NO_SHOW_LIMIT + 5)):
        _appointment(clinic_a, crm_a, now - timedelta(days=1, minutes=i), patient=patient)

    feed = build_feed(clinic_a, now=now)

    assert len(_of_kind(feed, NotificationKind.NO_SHOW)) == NO_SHOW_LIMIT
    assert feed.counts[NotificationKind.NO_SHOW] == NO_SHOW_LIMIT + 5
    assert feed.truncated is True


def test_sem_corte_a_contagem_e_o_tamanho_da_lista(clinic_a, crm_a):
    now = timezone.now()
    ana, bruno = _patients(clinic_a, 2)
    _appointment(clinic_a, crm_a, now - timedelta(days=1), patient=ana)
    _appointment(clinic_a, crm_a, now - timedelta(days=2), patient=bruno)

    feed = build_feed(clinic_a, now=now)

    assert feed.counts[NotificationKind.NO_SHOW] == 2
    assert feed.truncated is False


def test_status_aguardando_conta_como_sem_desfecho(clinic_a, crm_a):
    """
    `WAITING` entrou no projeto depois desta central. Os blocos de agenda
    derivam os status abertos do enum em vez de listá-los, justamente para um
    status novo não sumir em silêncio.
    """
    now = timezone.now()
    _appointment(
        clinic_a,
        crm_a,
        now - PENDING_OUTCOME_GRACE - timedelta(hours=1),
        status=AppointmentStatus.WAITING,
    )

    pendencias = _of_kind(build_feed(clinic_a, now=now), NotificationKind.PENDING_OUTCOME)

    assert len(pendencias) == 1
    assert pendencias[0].pill == "Ainda “Aguardando atendimento”"


def test_status_aguardando_aparece_na_agenda_de_hoje(clinic_a, crm_a):
    day_start = _local_day_start(timezone.now())
    now = day_start + timedelta(hours=8)
    _appointment(
        clinic_a,
        crm_a,
        day_start + timedelta(hours=12),
        status=AppointmentStatus.WAITING,
    )

    hoje = _of_kind(build_feed(clinic_a, now=now), NotificationKind.APPOINTMENT_TODAY)

    assert len(hoje) == 1


# ────────────────── faltas: o que ainda pede recuperação ──────────────────


def test_falta_com_consulta_remarcada_sai_da_lista(clinic_a, crm_a):
    """Remarcou é recuperado - insistir seria pedir um telefonema desnecessário."""
    now = timezone.now()
    _appointment(clinic_a, crm_a, now - timedelta(days=3))
    _confirmed(clinic_a, crm_a, now + timedelta(days=2))

    feed = build_feed(clinic_a, now=now)

    assert _of_kind(feed, NotificationKind.NO_SHOW) == []
    assert feed.counts[NotificationKind.NO_SHOW] == 0


def test_falta_com_retorno_ja_realizado_sai_da_lista(clinic_a, crm_a):
    now = timezone.now()
    _appointment(clinic_a, crm_a, now - timedelta(days=5))
    _appointment(
        clinic_a,
        crm_a,
        now - timedelta(days=2),
        status=AppointmentStatus.COMPLETED,
    )

    assert _of_kind(build_feed(clinic_a, now=now), NotificationKind.NO_SHOW) == []


def test_remarcacao_cancelada_nao_conta_como_recuperado(clinic_a, crm_a):
    now = timezone.now()
    _appointment(clinic_a, crm_a, now - timedelta(days=5))
    _appointment(
        clinic_a,
        crm_a,
        now - timedelta(days=2),
        status=AppointmentStatus.CANCELED,
    )

    assert len(_of_kind(build_feed(clinic_a, now=now), NotificationKind.NO_SHOW)) == 1


def test_faltar_de_novo_nao_conta_como_recuperado(clinic_a, crm_a):
    """Duas faltas seguidas viram uma linha - a mais recente."""
    now = timezone.now()
    _appointment(clinic_a, crm_a, now - timedelta(days=8))
    recente = _appointment(clinic_a, crm_a, now - timedelta(days=2))

    faltas = _of_kind(build_feed(clinic_a, now=now), NotificationKind.NO_SHOW)

    assert len(faltas) == 1
    assert faltas[0].target["appointment"] == recente.pk


def test_falta_de_um_paciente_nao_recupera_a_de_outro(clinic_a, crm_a):
    now = timezone.now()
    ana, bruno = _patients(clinic_a, 2)
    _appointment(clinic_a, crm_a, now - timedelta(days=3), patient=ana)
    _appointment(clinic_a, crm_a, now - timedelta(days=3), patient=bruno)
    # Só a Ana remarcou.
    _confirmed(clinic_a, crm_a, now + timedelta(days=1), patient=ana)

    faltas = _of_kind(build_feed(clinic_a, now=now), NotificationKind.NO_SHOW)

    assert len(faltas) == 1
    assert faltas[0].title == bruno.name


# ──────────────────────────────── ordenação ────────────────────────────────


def test_feed_sai_ordenado_por_severidade(clinic_a, crm_a):
    day_start = _local_day_start(timezone.now())
    now = day_start + timedelta(hours=8)

    _confirmed(clinic_a, crm_a, day_start + timedelta(hours=12))
    # Paciente à parte: quem tem consulta marcada hoje já foi recuperado e
    # sairia do bloco de faltas.
    (faltoso,) = _patients(clinic_a, 1)
    _appointment(clinic_a, crm_a, now - timedelta(days=1), patient=faltoso)
    SyncRun.objects.create(
        clinic=clinic_a,
        kind=SyncRunKind.APPOINTMENTS,
        started_at=now - timedelta(minutes=10),
        finished_at=now - timedelta(minutes=9),
        error="caiu",
    )

    assert _kinds(build_feed(clinic_a, now=now)) == [
        NotificationKind.SYNC_FAILED,
        NotificationKind.NO_SHOW,
        NotificationKind.APPOINTMENT_TODAY,
    ]


# ─────────────────────── invariante da marca d'água ───────────────────────


def test_occurred_at_nunca_e_futuro(clinic_a, crm_a):
    """
    A regra de não-lida é `occurred_at > read_at`. Um `occurred_at` no futuro
    deixaria o item não-lido para sempre, mesmo logo após marcar como lido.
    """
    day_start = _local_day_start(timezone.now())
    now = day_start + timedelta(hours=8)
    _confirmed(clinic_a, crm_a, day_start + timedelta(hours=20))

    feed = build_feed(clinic_a, now=now)

    assert feed.items
    assert all(item.occurred_at <= now for item in feed.items)


def test_marcar_como_lido_zera_o_nao_lido(clinic_a, crm_a):
    now = timezone.now()
    _appointment(clinic_a, crm_a, now - timedelta(days=1))
    feed = build_feed(clinic_a, now=now)

    assert feed.unread_count(read_at=None) == 1
    assert feed.unread_count(read_at=now) == 0


def test_evento_posterior_a_leitura_volta_a_ser_nao_lido(clinic_a, crm_a):
    now = timezone.now()
    read_at = now - timedelta(hours=3)
    _appointment(clinic_a, crm_a, now - timedelta(hours=1))

    assert build_feed(clinic_a, now=now).unread_count(read_at) == 1


# ────────────────────────────── escopo e API ──────────────────────────────


def test_feed_nao_vaza_entre_clinicas(clinic_a, clinic_b):
    now = timezone.now()
    crm_b = {
        "patient": Patient.objects.create(clinic=clinic_b, name="Paciente Beta"),
        "practitioner": Practitioner.objects.create(clinic=clinic_b, name="Dr. Beta"),
    }
    _appointment(clinic_b, crm_b, now - timedelta(days=1))
    SyncRun.objects.create(
        clinic=clinic_b,
        kind=SyncRunKind.APPOINTMENTS,
        started_at=now,
        finished_at=now,
        error="caiu",
    )

    assert build_feed(clinic_a, now=now).items == []
    assert len(build_feed(clinic_b, now=now).items) == 2


def test_get_exige_autenticacao(api_client):
    assert api_client.get(URL).status_code == 401


def test_get_retorna_o_feed(api_client, manager_single_clinic, clinic_a, crm_a):
    _appointment(clinic_a, crm_a, timezone.now() - timedelta(days=1))
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.get(URL)

    assert response.status_code == 200
    assert response.data["unread_count"] == 1
    assert response.data["read_at"] is None
    assert response.data["truncated"] is False

    item = response.data["results"][0]
    assert item["kind"] == NotificationKind.NO_SHOW
    assert item["severity"] == "warning"
    assert item["unread"] is True
    assert item["target"]["type"] == "patient"
    assert item["target"]["id"] == crm_a["patient"].pk


def test_counters_conta_por_bloco(api_client, manager_single_clinic, clinic_a, crm_a):
    _appointment(clinic_a, crm_a, timezone.now() - timedelta(days=1))
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.get(COUNTERS_URL)

    assert response.status_code == 200
    assert response.data["unread"] == 1
    assert response.data["total"] == 1
    assert response.data["by_kind"] == {
        NotificationKind.SYNC_FAILED: 0,
        NotificationKind.NO_SHOW: 1,
        NotificationKind.PENDING_OUTCOME: 0,
        NotificationKind.APPOINTMENT_TODAY: 0,
    }


def test_post_read_marca_e_zera_o_badge(api_client, manager_single_clinic, clinic_a, crm_a):
    _appointment(clinic_a, crm_a, timezone.now() - timedelta(days=1))
    api_client.force_authenticate(manager_single_clinic)

    response = api_client.post(READ_URL)

    assert response.status_code == 200
    assert response.data["unread_count"] == 0
    assert NotificationRead.objects.filter(clinic=clinic_a, user=manager_single_clinic).exists()

    assert api_client.get(COUNTERS_URL).data["unread"] == 0
    # O item continua na lista - lido não é o mesmo que resolvido.
    assert api_client.get(URL).data["results"][0]["unread"] is False


def test_post_read_repetido_nao_duplica_a_marca(api_client, manager_single_clinic, clinic_a):
    api_client.force_authenticate(manager_single_clinic)

    api_client.post(READ_URL)
    api_client.post(READ_URL)

    assert NotificationRead.objects.filter(clinic=clinic_a, user=manager_single_clinic).count() == 1


def test_marca_soft_deletada_e_revivida_e_nao_colide(api_client, manager_single_clinic, clinic_a):
    """
    `delete()` no projeto é soft, mas a unique (clinic, user) vale para a linha
    apagada também — com o manager padrão a remarcação estouraria constraint.
    """
    api_client.force_authenticate(manager_single_clinic)
    api_client.post(READ_URL)
    NotificationRead.objects.get(clinic=clinic_a, user=manager_single_clinic).delete()

    response = api_client.post(READ_URL)

    assert response.status_code == 200
    revived = NotificationRead.objects.get(clinic=clinic_a, user=manager_single_clinic)
    assert revived.deleted_at is None


def test_leitura_e_por_clinica(api_client, manager_two_clinics, clinic_a, clinic_b):
    """Quem atende em duas clínicas tem badge independente em cada uma."""
    api_client.force_authenticate(manager_two_clinics)

    api_client.post(READ_URL, HTTP_X_CLINIC_ID=str(clinic_a.pk))

    assert NotificationRead.objects.filter(clinic=clinic_a, user=manager_two_clinics).exists()
    assert not NotificationRead.objects.filter(clinic=clinic_b, user=manager_two_clinics).exists()


# ────────────────────────────────── LGPD ──────────────────────────────────


def test_payload_nao_carrega_conteudo_clinico(api_client, manager_single_clinic, clinic_a, crm_a):
    """
    §15 do levantamento: conteúdo clínico nunca vai para notificação.

    Nome, horário e profissional são logística e entram; procedimento e
    comentários do prontuário não podem aparecer em campo nenhum.
    """
    _appointment(
        clinic_a,
        crm_a,
        timezone.now() - timedelta(days=1),
        comments_html="<p>Paciente relatou dor lombar há três semanas</p>",
    )
    api_client.force_authenticate(manager_single_clinic)

    payload = str(api_client.get(URL).data)

    assert "dor lombar" not in payload
    assert "comments" not in payload
    assert "procedure" not in payload
