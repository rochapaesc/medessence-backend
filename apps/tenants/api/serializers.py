"""
Horário de funcionamento da clínica (§9.1, RF-FLW-5.1 e RF-FLW-18.4).

A semana INTEIRA é um recurso só. A tela edita os sete dias juntos e salva de
uma vez; um CRUD por faixa obrigaria o front a rastrear o que criou, editou e
apagou, e a mandar N requisições que podem falhar pelo meio, deixando a
clínica com meia semana cadastrada.
"""

from itertools import pairwise

from rest_framework.serializers import (
    IntegerField,
    ListField,
    Serializer,
    TimeField,
    ValidationError,
)

from apps.tenants.models.business_hours import Weekday


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
            raise ValidationError(
                f"{dia}: a hora de fechar precisa ser depois da hora de abrir."
            )
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
