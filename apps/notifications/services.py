"""
Derivação do feed da central de notificações (o sino da topbar).

Não existe tabela de notificações: o feed é montado a cada leitura a partir de
`Appointment` e `SyncRun`. A consequência boa é que o feed é sempre verdade no
instante da leitura - falta remarcada some sozinha, sync que voltou a funcionar
some sozinha - sem job de limpeza e sem linha órfã.

**Regra de não-lida:** `occurred_at > NotificationRead.read_at`. Para essa regra
fechar, `occurred_at` precisa estar SEMPRE no passado, senão o item nunca ficaria
lido. `_item()` trava isso no construtor.

**LGPD (§15 do levantamento):** conteúdo clínico nunca vai para notificação.
Nome, horário, profissional e unidade são logística e entram; procedimento,
comentários e qualquer dado do prontuário ficam de fora - nem são consultados.
"""

from dataclasses import dataclass
from datetime import datetime, timedelta

from django.db.models import Exists, F, OuterRef, Subquery
from django.utils import timezone

from apps.integrations.choices import SyncRunKind
from apps.integrations.models import SyncRun
from apps.notifications.choices import NotificationKind, NotificationSeverity
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import Appointment

# Janelas e tetos por bloco - o que sai daqui é o que o sino mostra.
# O teto corta a LISTA, não a contagem: o cabeçalho do bloco mostra o total
# real (ver `counts`), senão o número mentiria com o bloco fechado.
NO_SHOW_WINDOW = timedelta(days=30)
NO_SHOW_LIMIT = 20

PENDING_OUTCOME_WINDOW = timedelta(days=30)
# Carência: consulta que acabou de terminar não é pendência ainda.
PENDING_OUTCOME_GRACE = timedelta(hours=2)
PENDING_OUTCOME_LIMIT = 20

TODAY_LIMIT = 10

# Status terminais - a consulta já teve desfecho.
CLOSED_STATUSES = (
    AppointmentStatus.COMPLETED,
    AppointmentStatus.NO_SHOW,
    AppointmentStatus.CANCELED,
)

# Derivado, e não uma lista fixa: `WAITING` entrou no projeto depois da
# central, e com lista fixa as consultas nesse status sumiriam em silêncio dos
# dois blocos de agenda. Serve tanto para "ainda vai acontecer hoje" quanto
# para "já passou e ninguém fechou" - o que separa os dois é a janela de
# tempo, não o status.
OPEN_STATUSES = tuple(s for s in AppointmentStatus if s not in CLOSED_STATUSES)


@dataclass(frozen=True)
class NotificationItem:
    id: str
    kind: str
    severity: str
    title: str
    subtitle: str
    detail: str
    occurred_at: datetime
    target: dict
    # Rótulo curto de estado (vira AppPill no front). Vazio = sem pílula.
    # Composto aqui, e não no front, porque é o backend que decide o que pode
    # virar texto de notificação (LGPD).
    pill: str = ""
    # Texto da coluna da direita. Vazio = o front calcula o relativo a partir
    # de `occurred_at`. Só a agenda do dia preenche, com a hora da consulta:
    # ali `occurred_at` é a virada do dia e um relativo mediria a coisa errada.
    when_label: str = ""

    def as_dict(self, read_at: datetime | None) -> dict:
        return {
            "id": self.id,
            "kind": self.kind,
            "severity": self.severity,
            "title": self.title,
            "subtitle": self.subtitle,
            "detail": self.detail,
            "pill": self.pill,
            "when_label": self.when_label,
            "occurred_at": self.occurred_at,
            "unread": self.is_unread(read_at),
            "target": self.target,
        }

    def is_unread(self, read_at: datetime | None) -> bool:
        return read_at is None or self.occurred_at > read_at


@dataclass(frozen=True)
class Feed:
    items: list[NotificationItem]
    truncated: bool

    # Total real por bloco, antes do teto - é o que o cabeçalho do bloco mostra.
    # Contar `items` daria o número truncado, e com o bloco fechado esse número
    # é a única informação na tela.
    counts: dict[str, int]

    def unread_count(self, read_at: datetime | None) -> int:
        return sum(1 for item in self.items if item.is_unread(read_at))


@dataclass(frozen=True)
class Block:
    """Um bloco do feed: a lista já cortada pelo teto + quantos existem de fato."""

    items: list[NotificationItem]
    truncated: bool
    total: int


def _block(items: list[NotificationItem], truncated: bool, queryset) -> Block:
    # Só vai ao banco contar quando o teto cortou - sem corte o total é o
    # tamanho da própria lista.
    return Block(
        items=items,
        truncated=truncated,
        total=queryset.count() if truncated else len(items),
    )


def _item(*, occurred_at: datetime, now: datetime, **fields) -> NotificationItem:
    """
    Constrói o item travando a invariante da marca d'água.

    `occurred_at` nunca pode ser futuro: se fosse, `occurred_at > read_at`
    seria verdade mesmo logo após marcar como lido e o item ficaria não-lido
    para sempre.
    """
    return NotificationItem(occurred_at=min(occurred_at, now), **fields)


def _when(moment: datetime) -> str:
    return timezone.localtime(moment).strftime("%d/%m às %H:%M")


def _with(*parts: str) -> str:
    """Junta os pedaços presentes do subtítulo com o separador da casa."""
    return " · ".join(part for part in parts if part)


def _channel_down(clinic, now: datetime) -> list[NotificationItem]:
    """
    Canal de WhatsApp fora do ar (item 2 do fechamento do Inbox).

    Derivado do próprio `Channel`, sem tabela de notificação: é o mesmo padrão
    do bloco de sincronização, que lê o `SyncRun`. Enquanto o canal estiver
    morto o aviso fica; quando a credencial voltar, ele some sozinho.
    """
    from apps.inbox.models import Channel

    canal = Channel.objects.filter(clinic=clinic).exclude(disconnected_at=None).first()
    if canal is None:
        return []
    return [
        _item(
            now=now,
            occurred_at=canal.disconnected_at,
            id=f"{NotificationKind.CHANNEL_DOWN}:{canal.pk}",
            kind=NotificationKind.CHANNEL_DOWN,
            severity=NotificationSeverity.DANGER,
            title="WhatsApp desconectado",
            subtitle=f"A clínica parou de responder {_when(canal.disconnected_at)}",
            detail=canal.disconnect_reason,
            target={"type": "channel", "id": canal.pk},
        )
    ]


def _sync_failures(clinic, now: datetime) -> list[NotificationItem]:
    """Último run de cada tipo que terminou em erro.

    `SyncRun` não tem campo de status - falha é `error != ""`, mesma derivação
    que `EHRSyncView._run_state` faz para o botão da topbar.

    Um SELECT por tipo, como o `EHRSyncView`: `DISTINCT ON` resolveria em uma
    query só, mas é exclusivo do Postgres e os testes rodam em SQLite.
    """
    items = []
    for kind in SyncRunKind:
        run = (
            SyncRun.objects.filter(clinic=clinic, kind=kind)
            .order_by(F("started_at").desc(nulls_last=True))
            .first()
        )
        if run is None or not run.error:
            continue
        moment = run.finished_at or run.started_at or run.created_at
        items.append(
            _item(
                now=now,
                occurred_at=moment,
                id=f"{NotificationKind.SYNC_FAILED}:{kind.value}",
                kind=NotificationKind.SYNC_FAILED,
                severity=NotificationSeverity.DANGER,
                title=f"Falha ao sincronizar {kind.label}",
                subtitle=f"Tentativa automática {_when(moment)}",
                detail=run.error,
                target={"type": "sync", "kind": kind.value},
            )
        )
    items.sort(key=lambda item: item.occurred_at, reverse=True)
    return items


def _no_shows(clinic, now: datetime) -> Block:
    """
    Faltas que ainda pedem ação - a lista de recuperação.

    "Para recuperar" é mais estreito que "faltou", e a diferença são duas
    exclusões:

    - **Já voltou ou já remarcou.** Qualquer consulta do paciente posterior à
      falta que não seja outra falta nem um cancelamento significa que a
      recuperação já aconteceu - listar de novo seria pedir um telefonema
      desnecessário.
    - **Uma linha por paciente.** Quem faltou três vezes precisa de um contato,
      não de três; fica a falta mais recente.
    """
    window_start = now - NO_SHOW_WINDOW

    # A falta mais recente de cada paciente dentro da janela.
    latest_per_patient = (
        Appointment.objects.filter(
            clinic=clinic,
            patient_id=OuterRef("patient_id"),
            status=AppointmentStatus.NO_SHOW,
            starts_at__gte=window_start,
            starts_at__lte=now,
        )
        .order_by("-starts_at")
        .values("pk")[:1]
    )

    returned = Appointment.objects.filter(
        clinic=clinic,
        patient_id=OuterRef("patient_id"),
        starts_at__gt=OuterRef("starts_at"),
    ).exclude(status__in=(AppointmentStatus.CANCELED, AppointmentStatus.NO_SHOW))

    queryset = (
        Appointment.objects.filter(
            clinic=clinic,
            status=AppointmentStatus.NO_SHOW,
            starts_at__gte=window_start,
            starts_at__lte=now,
            pk=Subquery(latest_per_patient),
        )
        .annotate(has_return=Exists(returned))
        .filter(has_return=False)
        .select_related("patient", "practitioner")
        .order_by("-starts_at")
    )

    rows = list(queryset[: NO_SHOW_LIMIT + 1])
    truncated = len(rows) > NO_SHOW_LIMIT

    items = [
        _item(
            now=now,
            occurred_at=appointment.starts_at,
            id=f"{NotificationKind.NO_SHOW}:{appointment.pk}",
            kind=NotificationKind.NO_SHOW,
            severity=NotificationSeverity.WARNING,
            # Só o nome: o bloco já diz "faltas" e a pílula já diz "Faltou" -
            # repetir o verbo no título estourava a linha com nome longo.
            title=appointment.patient.name,
            subtitle=_with(_when(appointment.starts_at), appointment.practitioner.name),
            detail="",
            pill=AppointmentStatus.NO_SHOW.label,
            target={
                "type": "patient",
                "id": appointment.patient_id,
                "appointment": appointment.pk,
            },
        )
        for appointment in rows[:NO_SHOW_LIMIT]
    ]
    return _block(items, truncated, queryset)


def _pending_outcome(clinic, now: datetime) -> Block:
    """
    Consultas que já passaram e ninguém fechou - seguem em `scheduled`/`confirmed`.

    A carência evita transformar a consulta que acabou de terminar em pendência.
    """
    cutoff = now - PENDING_OUTCOME_GRACE
    queryset = (
        Appointment.objects.filter(
            clinic=clinic,
            status__in=OPEN_STATUSES,
            starts_at__lt=cutoff,
            starts_at__gte=now - PENDING_OUTCOME_WINDOW,
        )
        .select_related("patient", "practitioner")
        .order_by("-starts_at")
    )

    rows = list(queryset[: PENDING_OUTCOME_LIMIT + 1])
    truncated = len(rows) > PENDING_OUTCOME_LIMIT

    items = []
    for appointment in rows[:PENDING_OUTCOME_LIMIT]:
        ends_at = appointment.starts_at + timedelta(minutes=appointment.duration_min)
        items.append(
            _item(
                now=now,
                occurred_at=ends_at,
                id=f"{NotificationKind.PENDING_OUTCOME}:{appointment.pk}",
                kind=NotificationKind.PENDING_OUTCOME,
                severity=NotificationSeverity.WARNING,
                title=appointment.patient.name,
                subtitle=_with(_when(appointment.starts_at), appointment.practitioner.name),
                detail="",
                pill=f"Ainda “{appointment.get_status_display()}”",
                target={
                    "type": "appointment",
                    "id": appointment.pk,
                    "date": timezone.localtime(appointment.starts_at).date().isoformat(),
                },
            )
        )
    return _block(items, truncated, queryset)


def _today(clinic, now: datetime) -> Block:
    """
    O que ainda vai acontecer hoje - consulta que já passou é ruído, não aviso.

    `occurred_at` aqui NÃO é `starts_at`: ele é futuro e o item nunca ficaria
    lido. Ancorar na virada do dia local faz a agenda reaparecer como novidade
    toda manhã; o `max` com `created_at` faz a consulta marcada às 15h para as
    17h contar como novidade na hora.
    """
    local_now = timezone.localtime(now)
    start_of_day = local_now.replace(hour=0, minute=0, second=0, microsecond=0)
    end_of_day = start_of_day + timedelta(days=1)

    queryset = (
        Appointment.objects.filter(
            clinic=clinic,
            status__in=OPEN_STATUSES,
            starts_at__gte=now,
            starts_at__lt=end_of_day,
        )
        .select_related("patient", "practitioner", "care_unit")
        .order_by("starts_at")
    )

    rows = list(queryset[: TODAY_LIMIT + 1])
    truncated = len(rows) > TODAY_LIMIT

    items = [
        _item(
            now=now,
            occurred_at=max(start_of_day, appointment.created_at),
            id=f"{NotificationKind.APPOINTMENT_TODAY}:{appointment.pk}",
            kind=NotificationKind.APPOINTMENT_TODAY,
            severity=NotificationSeverity.INFO,
            title=appointment.patient.name,
            subtitle=_with(
                appointment.practitioner.name,
                appointment.care_unit.name if appointment.care_unit else "",
            ),
            detail="",
            # A hora da consulta vai na coluna da direita, no lugar do relativo.
            when_label=f"{timezone.localtime(appointment.starts_at):%H:%M}",
            target={
                "type": "agenda",
                "date": local_now.date().isoformat(),
                "appointment": appointment.pk,
            },
        )
        for appointment in rows[:TODAY_LIMIT]
    ]
    return _block(items, truncated, queryset)


def build_feed(clinic, now: datetime | None = None) -> Feed:
    """Monta o feed da clínica, já ordenado por severidade (não por horário)."""
    now = now or timezone.now()

    canal = _channel_down(clinic, now)
    sync = _sync_failures(clinic, now)
    blocks = [
        Block(items=canal, truncated=False, total=len(canal)),
        Block(items=sync, truncated=False, total=len(sync)),
        _no_shows(clinic, now),
        _pending_outcome(clinic, now),
        _today(clinic, now),
    ]

    counts = {kind.value: 0 for kind in NotificationKind}
    for block in blocks:
        if block.items:
            counts[block.items[0].kind] = block.total

    return Feed(
        items=[item for block in blocks for item in block.items],
        truncated=any(block.truncated for block in blocks),
        counts=counts,
    )


def get_read_at(clinic, user) -> datetime | None:
    """Até quando este usuário já viu as notificações desta clínica."""
    from apps.notifications.models import NotificationRead

    read = NotificationRead.objects.filter(clinic=clinic, user=user).first()
    return read.read_at if read else None


def mark_read(clinic, user, now: datetime | None = None) -> datetime:
    """Move a marca d'água para agora. Idempotente."""
    from apps.notifications.models import NotificationRead

    now = now or timezone.now()
    # `all_objects` porque a unique é (clinic, user) mesmo em linha soft-deletada:
    # com o manager padrão um registro apagado viraria colisão na hora de criar.
    NotificationRead.all_objects.update_or_create(
        clinic=clinic,
        user=user,
        defaults={"read_at": now, "deleted_at": None},
    )
    return now
