"""
API do horário de funcionamento (§9.1, RF-FLW-18.4).

  GET  /clinic/business-hours/  : a semana da clínica ativa, mais o fuso.
  PUT  /clinic/business-hours/  : substitui a semana inteira.

Nasceu em 05/08/2026. Até então `ClinicBusinessHours` existia só no admin do
Django, e o fluxo marcado como "só fora do horário" atendia a qualquer hora
porque ninguém tinha como cadastrar o expediente.
"""

from django.db import transaction
from rest_framework.generics import RetrieveUpdateAPIView
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api.permissions import IsClinicManager
from apps.core.audit import log_action
from apps.core.context import resolve_active_membership
from apps.core.models.audit_log import AuditAction
from apps.tenants.api.serializers import (
    ClinicBusinessHoursSerializer,
    ClinicSettingsSerializer,
)
from apps.tenants.models import ClinicBusinessHours


class ClinicSettingsView(RetrieveUpdateAPIView):
    """
    As configurações da clínica ativa (§4.13, RF-CFG-2).

    Só o GESTOR, e para ler também: o que se muda aqui alcança a clínica
    inteira. O nome sai nas mensagens ao paciente, o fuso decide quando a
    sequência dispara e a janela redefine quem é paciente ativo.
    """

    permission_classes = [IsClinicManager]
    serializer_class = ClinicSettingsSerializer
    http_method_names = ["get", "patch", "options", "head"]

    def get_object(self):
        return resolve_active_membership(self.request).clinic

    def perform_update(self, serializer):
        antes = {campo: getattr(serializer.instance, campo) for campo in _AUDITADOS}
        clinic = serializer.save()
        mudou = [c for c in _AUDITADOS if antes[c] != getattr(clinic, c)]
        if not mudou:
            return
        # RF-CFG-2.5: trocar a janela de atividade muda a fila de resgate da
        # clínica inteira, e quando o número muda sozinho a primeira pergunta é
        # quem mexeu. Os VALORES entram porque nenhum deles é sensível, e sem
        # eles o registro não responde "mudou de quanto para quanto".
        log_action(
            self.request.user,
            AuditAction.UPDATE,
            "Clinic",
            clinic.pk,
            payload={
                "changed_fields": mudou,
                **{f"{c}_antes": antes[c] for c in mudou},
                **{f"{c}_depois": getattr(clinic, c) for c in mudou},
            },
            request=self.request,
            clinic=clinic,
        )


_AUDITADOS = ("name", "timezone", "active_window_days")


class ClinicBusinessHoursView(APIView):
    """
    Só o GESTOR, e para ler também.

    O horário decide se o robô atende ou se a conversa vai para a recepção
    (RF-FLW-5.1). Quem pode mexer nele muda o comportamento do atendimento
    para todo paciente que escrever fora do expediente, o que é a mesma
    régua aplicada aos fluxos.
    """

    permission_classes = [IsClinicManager]
    serializer_class = ClinicBusinessHoursSerializer

    def get(self, request):
        clinic = resolve_active_membership(request).clinic
        return Response(self._payload(clinic))

    @transaction.atomic
    def put(self, request):
        """
        Substitui a semana inteira, numa transação.

        ⚠️ Apaga de verdade, e não por `deleted_at`. Horário é CONFIGURAÇÃO e
        não registro: ninguém vai perguntar qual era o expediente em março, e
        nada aponta para a faixa antiga. Com soft delete, cada salvamento
        deixaria sete linhas mortas para trás, e a tabela cresceria sem teto
        por um dado que o gestor ajusta mexendo e remexendo.
        """
        clinic = resolve_active_membership(request).clinic
        serializer = self.serializer_class(data=request.data)
        serializer.is_valid(raise_exception=True)

        ClinicBusinessHours.all_objects.filter(clinic=clinic).hard_delete()
        ClinicBusinessHours.objects.bulk_create(
            [
                ClinicBusinessHours(clinic=clinic, **faixa)
                for faixa in serializer.validated_data["hours"]
            ]
        )
        return Response(self._payload(clinic))

    def _payload(self, clinic) -> dict:
        """
        O fuso vai junto porque a tela precisa dizer em que relógio aquelas
        horas valem. Sem isso, quem cadastra de outro estado digita o horário
        errado e só descobre pelo paciente que não foi atendido.
        """
        faixas = ClinicBusinessHours.objects.filter(clinic=clinic).order_by("weekday", "opens_at")
        return {
            "timezone": clinic.timezone,
            "hours": [
                {
                    "weekday": f.weekday,
                    "opens_at": f.opens_at.strftime("%H:%M"),
                    "closes_at": f.closes_at.strftime("%H:%M"),
                }
                for f in faixas
            ],
        }
