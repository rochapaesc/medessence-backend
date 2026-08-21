"""
Horário de funcionamento da clínica (§9.1, RF-FLW-5.1 e RF-FLW-18.4).

A semana INTEIRA é um recurso só. A tela edita os sete dias juntos e salva de
uma vez; um CRUD por faixa obrigaria o front a rastrear o que criou, editou e
apagou, e a mandar N requisições que podem falhar pelo meio, deixando a
clínica com meia semana cadastrada.
"""

from itertools import pairwise
from zoneinfo import ZoneInfo

from rest_framework.serializers import (
    IntegerField,
    ListField,
    ModelSerializer,
    Serializer,
    TimeField,
    ValidationError,
)

from apps.tenants.models import Clinic
from apps.tenants.models.business_hours import Weekday


class ClinicSettingsSerializer(ModelSerializer):
    """
    As configurações da clínica (§4.13, RF-CFG-2).

    ⚠️ TRÊS campos, e só eles. O que o modelo tem além disto fica de fora com
    motivo: `slug` é identificador (muda comando e endereço), as credenciais do
    EHR são da plataforma e ficam cifradas, `ehr_push_enabled` é trava de
    segurança que se liga por comando depois de validar a leitura, e
    `appointments_backfilled_until` é marca-d'água interna do backfill.
    """

    class Meta:
        model = Clinic
        fields = ["id", "name", "slug", "timezone", "active_window_days"]
        read_only_fields = ["id", "slug"]

    def validate_name(self, value):
        value = (value or "").strip()
        if not value:
            raise ValidationError("A clínica precisa de um nome.")
        return value

    def validate_timezone(self, value):
        """
        ⚠️ Fuso inválido não pode entrar: ele é lido pelo disparo da sequência,
        pela contagem da agenda e pelo horário de funcionamento, e um valor que
        o `ZoneInfo` não conhece quebraria os três de uma vez, longe daqui.
        """
        value = (value or "").strip()
        # Uma guarda só: o `ZoneInfo` é quem recusa de verdade. Conferir antes
        # contra `available_timezones()` era código que nunca chegava a agir,
        # e a prova negativa mostrou isso ao desligá-lo sem nada quebrar.
        try:
            ZoneInfo(value)
        except Exception as exc:
            raise ValidationError(f"'{value}' não é um fuso horário conhecido.") from exc
        return value

    def validate_active_window_days(self, value):
        # A faixa existe para barrar o dedo errado: 1 dia deixaria a clínica
        # inteira inativa amanhã, e 3.650 faria "ativo" perder o sentido.
        if not (7 <= value <= 1095):
            raise ValidationError("Escolha entre 7 e 1095 dias.")
        return value


class BusinessHoursRangeSerializer(Serializer):
    """Uma faixa de atendimento: dia, hora de abrir e hora de fechar."""

    weekday = IntegerField(min_value=0, max_value=6)
    opens_at = TimeField()
    closes_at = TimeField()

    def validate(self, attrs):
        # Fechar antes de abrir deixaria o dia fechado o tempo todo sem
        # ninguém entender por quê: nenhuma hora cai dentro do intervalo.
        if attrs["closes_at"] <= attrs["opens_at"]:
            dia = Weekday(attrs["weekday"]).label
            raise ValidationError(f"{dia}: a hora de fechar precisa ser depois da hora de abrir.")
        return attrs


class ClinicBusinessHoursSerializer(Serializer):
    """A semana toda. Lista vazia é legítima: significa clínica sem horário."""

    hours = ListField(child=BusinessHoursRangeSerializer(), allow_empty=True)

    def validate_hours(self, value):
        """
        Faixas do mesmo dia não podem se sobrepor.

        Duas faixas cobrindo a mesma hora dariam duas respostas para "a
        clínica está aberta agora", e a de trás nunca seria consultada. O erro
        nomeia o dia porque a lista chega toda junta e "faixa 3" não diz nada
        para quem está olhando a grade.
        """
        por_dia: dict[int, list] = {}
        for faixa in value:
            por_dia.setdefault(faixa["weekday"], []).append(faixa)

        for weekday, faixas in por_dia.items():
            faixas.sort(key=lambda f: f["opens_at"])
            for anterior, seguinte in pairwise(faixas):
                if seguinte["opens_at"] < anterior["closes_at"]:
                    raise ValidationError(
                        f"{Weekday(weekday).label}: dois horários se sobrepõem. "
                        f"O de {seguinte['opens_at']:%H:%M} começa antes de o "
                        f"anterior fechar, às {anterior['closes_at']:%H:%M}."
                    )
        return value
