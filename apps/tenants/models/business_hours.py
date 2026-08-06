from django.db.models import (
    CASCADE,
    ForeignKey,
    IntegerChoices,
    PositiveSmallIntegerField,
    Q,
    TimeField,
    UniqueConstraint,
)

from apps.core.models import BaseModel


class Weekday(IntegerChoices):
    """
    Segunda = 0, como o `weekday()` do Python. A escolha é para o motor não
    precisar converter nada na hora de decidir se a clínica está aberta.
    """

    MONDAY = 0, "Segunda-feira"
    TUESDAY = 1, "Terça-feira"
    WEDNESDAY = 2, "Quarta-feira"
    THURSDAY = 3, "Quinta-feira"
    FRIDAY = 4, "Sexta-feira"
    SATURDAY = 5, "Sábado"
    SUNDAY = 6, "Domingo"


class ClinicBusinessHours(BaseModel):
    """
    Horário de funcionamento da clínica (§9.1, RF-FLW-5.1.1).

    NASCEU com a F2.6 - até 31/07/2026 a clínica não tinha horário nenhum,
    só `timezone`. Quem precisou dele foi o fluxo: "fora do expediente o robô
    atende, dentro dele a conversa vai para a recepção" é a regra que faz o
    atendimento automático valer para uma recepção de uma ou duas pessoas.

    Uma linha por FAIXA de atendimento, e não um par abre/fecha no `Clinic`,
    porque clínica abre sábado de manhã e fecha domingo - um par único não
    representa isso. **Dia sem linha = fechado o dia inteiro**, que é o
    default certo: clínica nova não atende de madrugada por omissão.

    ⚠️ **Vários intervalos no MESMO dia** (05/08/2026). Nasceu com um por dia,
    e isso não representava a clínica que fecha para o almoço: ela teria de
    cadastrar 08:00 às 18:00, e o sistema a consideraria aberta justamente nas
    duas horas em que não há ninguém na recepção para receber o paciente. Vale
    igual para turno partido, comum onde o médico divide agenda com hospital.
    A restrição de um por dia caiu; ficou a de não repetir a MESMA hora de
    abertura, e a sobreposição é barrada na validação, que enxerga o intervalo
    inteiro.

    Avaliado sempre no `clinic.timezone`. Comparar com o relógio do servidor
    poria a clínica de Fortaleza abrindo às 5h para o motor.
    """

    clinic = ForeignKey(
        "tenants.Clinic",
        verbose_name="Clínica",
        on_delete=CASCADE,
        related_name="business_hours",
    )
    weekday = PositiveSmallIntegerField(verbose_name="Dia da semana", choices=Weekday.choices)
    opens_at = TimeField(verbose_name="Abre às")
    closes_at = TimeField(verbose_name="Fecha às")

    class Meta:
        verbose_name = "Horário de funcionamento"
        verbose_name_plural = "Horários de funcionamento"
        ordering = ["weekday", "opens_at"]
        constraints = [
            # Rede de segurança contra a duplicata exata. A sobreposição de
            # verdade (08:00 às 12:00 com 09:00 às 13:00) é barrada na
            # validação, que compara o intervalo inteiro e não só o começo.
            UniqueConstraint(
                fields=["clinic", "weekday", "opens_at"],
                condition=Q(deleted_at__isnull=True),
                name="uniq_business_hours_faixa",
            ),
        ]

    def __str__(self):
        return f"{self.get_weekday_display()} {self.opens_at:%H:%M} às {self.closes_at:%H:%M}"
