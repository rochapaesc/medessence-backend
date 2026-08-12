from datetime import timedelta

from django.db.models import (
    CharField,
    DateField,
    DateTimeField,
    DecimalField,
    EmailField,
    JSONField,
    Max,
    Min,
    Q,
    TextField,
    UniqueConstraint,
)
from django.utils import timezone

from apps.core.choices import SyncStatus
from apps.core.managers import ActiveManager
from apps.core.models import TenantScopedModel
from apps.core.querysets import SoftDeleteQuerySet
from apps.patients.choices import Gender, PatientSource, PatientStatus

# RF-PAC-2 (decisão 09/07/2026): ativo = consulta nos últimos N dias; inativo
# = acima disso (ou nunca consultou). N é CONFIGURÁVEL: padrão da clínica
# (Clinic.active_window_days, default 90) com override por profissional
# (Practitioner.active_window_days) na visão da carteira.
DEFAULT_ACTIVE_WINDOW_DAYS = 90

# RF-REA-1.2: faixas por tempo desde a última consulta, em dias.
#
# Cobrem a base INTEIRA (decisão de 11/08/2026, quando a tela deixou de partir
# só da fila de resgate): sem `0_3` e `never`, a soma das faixas não fecharia
# com o total e a tela mostraria um "Todos" maior que a soma do que está
# embaixo dele, sem explicar a diferença.
#
# `12_plus` é ABERTA em cima de propósito: o espelho da agenda alcança setembro
# de 2023, então uma faixa "mais de 2 anos" teria UM paciente na clínica real,
# e faixa com um item dentro parece defeito, não recorte.
ABSENCE_RANGES = {
    "0_3": (0, 90),
    "3_6": (90, 180),
    "6_12": (180, 365),
    "12_plus": (365, None),
    #: Sem consulta nenhuma no espelho. Não é intervalo de dias, e por isso
    #: `by_absence` o trata à parte.
    "never": None,
}


def active_cutoff(window_days: int = DEFAULT_ACTIVE_WINDOW_DAYS, now=None):
    return (now or timezone.now()) - timedelta(days=window_days)


class PatientQuerySet(SoftDeleteQuerySet):
    def by_status(
        self,
        status: str,
        window_days: int = DEFAULT_ACTIVE_WINDOW_DAYS,
        practitioner=None,
    ):
        """
        Filtra por status calculado (RF-PAC-2). ATIVO = compareceu dentro da
        janela OU tem consulta futura agendada (retorno marcado não vira alvo
        de reativação). INATIVO = o complemento. Sem `practitioner`, usa os
        denormalizados da clínica; com ele, a atividade é relativa à CARTEIRA
        dele (última/próxima consulta COM ELE).
        """
        now = timezone.now()
        cutoff = active_cutoff(window_days, now)

        if practitioner is not None:
            queryset = self._annotate_practitioner(practitioner, now)
            active = Q(pract_last__gte=cutoff) | Q(pract_next__isnull=False)
        else:
            queryset = self
            active = Q(last_appointment_at__gte=cutoff) | Q(next_appointment_at__gte=now)

        if status == PatientStatus.ACTIVE:
            return queryset.filter(active)
        if status == PatientStatus.INACTIVE:
            return queryset.exclude(active)
        return queryset

    def _annotate_practitioner(self, practitioner, now):
        """Anota última consulta passada e próxima futura COM o profissional."""
        base = (
            Q(appointments__practitioner=practitioner)
            & Q(appointments__deleted_at__isnull=True)
            & ~Q(appointments__status__in=["canceled", "no_show"])
        )
        return self.annotate(
            pract_last=Max(
                "appointments__starts_at",
                filter=base & Q(appointments__starts_at__lte=now),
            ),
            pract_next=Min(
                "appointments__starts_at",
                filter=base & Q(appointments__starts_at__gt=now),
            ),
        )

    def to_reactivate(
        self,
        window_days: int = DEFAULT_ACTIVE_WINDOW_DAYS,
        practitioner=None,
    ):
        """
        A FILA DE RESGATE (RF-REA-1.1): inativo que JÁ compareceu alguma vez.

        Quem nunca teve consulta espelhada é cadastro sem histórico, e disparo
        pago para ele é prospecção, não resgate - na clínica real são 2.762 dos
        4.653 inativos, mais da metade.

        ⚠️ Este método é a definição ÚNICA do recorte, e o contador e a
        listagem saem os dois daqui. Era exatamente o defeito da tela de
        Reativação: `status_counters` separava os dois grupos e a listagem só
        sabia filtrar `status=inactive`, então o número do topo nunca poderia
        corresponder à lista embaixo dele.
        """
        inactive = self.by_status(PatientStatus.INACTIVE, window_days, practitioner)
        has_history = (
            Q(pract_last__isnull=False)
            if practitioner is not None
            else Q(last_appointment_at__isnull=False)
        )
        return inactive.filter(has_history)

    def com_ultimo_profissional(self):
        """
        Anota o profissional da última consulta de cada paciente.

        Só é encadeado onde a tela precisa (a fila de resgate), e não no
        queryset padrão: é uma subquery por linha, e as outras listagens de
        paciente pagariam por um dado que não mostram.

        ⚠️ O `Patient` guarda `last_appointment_at` denormalizado mas NÃO
        guarda com quem foi. Denormalizar o nome junto seria mais barato de
        ler e mais caro de manter: mudou o nome do profissional, todas as
        linhas ficariam com o nome velho até alguém reprocessar.
        """
        from django.db.models import OuterRef, Subquery

        ultima = (
            self.model.appointments.rel.related_model.objects.filter(
                patient=OuterRef("pk"),
                deleted_at__isnull=True,
            )
            .exclude(status__in=["canceled", "no_show"])
            .order_by("-starts_at")
        )
        return self.annotate(
            last_practitioner_name=Subquery(
                ultima.values("practitioner__name")[:1]
            ),
        )

    def by_absence(self, faixa: str, practitioner=None, now=None):
        """
        Faixa por tempo desde a última consulta (RF-REA-1.2).

        Faixa desconhecida não filtra nada. Pela API isso não acontece (o
        `ChoiceFilter` recusa com 400 antes de chegar aqui); a guarda existe
        para quem chama o queryset direto, onde devolver a lista inteira é
        menos surpreendente do que estourar.

        ⚠️ Com `practitioner` o corte é na anotação `pract_last`, que nasce no
        `by_status`. Este método NÃO assume que ela já existe: o django-filter
        aplica os filtros na ordem em que declara os campos, e depender dessa
        ordem para `?absence=` vir depois de `?segment=` seria um erro que só
        aparece quando alguém trocar duas linhas de lugar.
        """
        if faixa not in ABSENCE_RANGES:
            return self
        now = now or timezone.now()

        queryset = self
        campo = "last_appointment_at"
        if practitioner is not None:
            campo = "pract_last"
            if campo not in self.query.annotations:
                queryset = self._annotate_practitioner(practitioner, now)

        # "Nunca consultou" não é intervalo: é a ausência do dado.
        if faixa == "never":
            return queryset.filter(**{f"{campo}__isnull": True})

        desde, ate = ABSENCE_RANGES[faixa]
        filtro = Q(**{f"{campo}__lt": now - timedelta(days=desde)})
        if ate is not None:
            filtro &= Q(**{f"{campo}__gte": now - timedelta(days=ate)})
        return queryset.filter(filtro)

    def status_counters(
        self,
        window_days: int = DEFAULT_ACTIVE_WINDOW_DAYS,
        practitioner=None,
    ) -> dict:
        """
        Contadores (RF-PAC-1/RF-DSH-1). `to_reactivate` (lapsado) = inativo que
        JÁ compareceu antes - exclui quem nunca teve consulta (novo/sem
        histórico), removendo o viés no número de reativação. Por isso
        `active + inactive = total`, mas `to_reactivate ≤ inactive`.
        """
        return {
            "total": self.count(),
            PatientStatus.ACTIVE.value: self.by_status(
                PatientStatus.ACTIVE, window_days, practitioner
            ).count(),
            PatientStatus.INACTIVE.value: self.by_status(
                PatientStatus.INACTIVE, window_days, practitioner
            ).count(),
            "to_reactivate": self.to_reactivate(window_days, practitioner).count(),
        }


class PatientManager(ActiveManager):
    def get_queryset(self):
        return PatientQuerySet(self.model, using=self._db).alive()


class Patient(TenantScopedModel):
    """
    Paciente do CRM. Demográficos têm o EHR como dono (§10.1): edição local
    vira push (fase do adapter); pull sobrescreve. `last_appointment_at` é
    denormalizado da agenda e deriva o status ativo/inativo (90 dias).
    """

    name = CharField(verbose_name="Nome", max_length=200)
    cpf = CharField(
        verbose_name="Documento (CPF)",
        max_length=32,
        blank=True,
        db_index=True,
        help_text="personalIdentifier do EHR - pode ser CPF, DNI ou passaporte.",
    )
    birth_date = DateField(verbose_name="Nascimento", null=True, blank=True)
    gender = CharField(
        verbose_name="Gênero", max_length=10, choices=Gender.choices, default=Gender.UNKNOWN
    )
    email = EmailField(verbose_name="Email", blank=True)
    phone = CharField(
        verbose_name="Telefone",
        max_length=20,
        blank=True,
        db_index=True,
        help_text="E.164 - vincula o Contact do WhatsApp.",
    )
    city = CharField(verbose_name="Cidade", max_length=120, blank=True)
    state = CharField(verbose_name="UF", max_length=2, blank=True)
    address = JSONField(verbose_name="Endereço", default=dict, blank=True)
    profession = CharField(verbose_name="Profissão", max_length=120, blank=True)
    blood_type = CharField(verbose_name="Tipo sanguíneo", max_length=3, blank=True)
    weight_kg = DecimalField(
        verbose_name="Peso (kg)", max_digits=5, decimal_places=1, null=True, blank=True
    )
    height_cm = DecimalField(
        verbose_name="Altura (cm)", max_digits=5, decimal_places=1, null=True, blank=True
    )
    guardians = JSONField(
        verbose_name="Responsáveis",
        default=dict,
        blank=True,
        help_text='{"mother": {"name", "phone"}, "father": ..., "partner": ..., "sponsor": ...}',
    )
    emergency_contacts = JSONField(
        verbose_name="Contatos de emergência", default=list, blank=True
    )
    birth_info = JSONField(
        verbose_name="Dados do nascimento",
        default=dict,
        blank=True,
        help_text="Pediatria: idade gestacional, peso/altura ao nascer, perímetro cefálico, ordem.",
    )
    comments_html = TextField(
        verbose_name="Observações",
        blank=True,
        help_text="HTML sanitizado na entrada (whitelist).",
    )
    insurance_name = CharField(verbose_name="Convênio (texto)", max_length=120, blank=True)
    source = CharField(
        verbose_name="Origem",
        max_length=10,
        choices=PatientSource.choices,
        default=PatientSource.LOCAL,
    )
    external_id = CharField(verbose_name="ID no EHR", max_length=64, blank=True)
    raw_payload = JSONField(verbose_name="Payload cru", default=dict, blank=True)
    last_appointment_at = DateTimeField(
        verbose_name="Última consulta",
        null=True,
        blank=True,
        db_index=True,
        help_text="Denormalizado da agenda - deriva o status ativo/inativo (90 dias).",
    )
    next_appointment_at = DateTimeField(
        verbose_name="Próxima consulta",
        null=True,
        blank=True,
        db_index=True,
        help_text=(
            "Denormalizado da agenda - próxima consulta futura (não cancelada). "
            "Um retorno agendado mantém o paciente ativo, sem viés de reativação."
        ),
    )
    sync_status = CharField(
        verbose_name="Sincronização",
        max_length=10,
        choices=SyncStatus.choices,
        default=SyncStatus.SYNCED,
    )

    objects = PatientManager()

    class Meta:
        verbose_name = "Paciente"
        verbose_name_plural = "Pacientes"
        constraints = [
            UniqueConstraint(
                fields=["clinic", "external_id"],
                condition=~Q(external_id=""),
                name="uniq_patient_ext",
            ),
        ]

    def __str__(self):
        return self.name

    def status_for_window(self, window_days: int) -> str:
        """
        Status calculado (RF-PAC-2): ATIVO se compareceu na janela OU tem
        consulta futura agendada; senão INATIVO.
        """
        now = timezone.now()
        attended = self.last_appointment_at and self.last_appointment_at >= active_cutoff(
            window_days, now
        )
        booked = self.next_appointment_at and self.next_appointment_at >= now
        return PatientStatus.ACTIVE if (attended or booked) else PatientStatus.INACTIVE

    @property
    def status(self) -> str:
        """
        Status pela janela DA CLÍNICA. Em listagens, prefira
        `status_for_window` com a janela já resolvida (evita query por linha).
        """
        return self.status_for_window(self.clinic.active_window_days)
