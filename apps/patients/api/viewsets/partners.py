"""
Área de Parceiros (RF-PAR, §4.11) - a agenda dos atendimentos REALIZADOS.

A tela lista os pacientes atendidos num dia, direto da agenda espelhada, e
**não toca no EHR para isso**. O prontuário é buscado só quando alguém abre um
paciente, e quem faz isso é a própria ficha, sob demanda.

Foi um redesenho, e o motivo está no §18: a versão anterior montava a lista a
partir do espelho clínico, o que obrigava a perguntar à vSaúde paciente por
paciente quem tinha receita - 2 chamadas e ~1,09 s cada, cronometrado. Um dia
com 44 atendimentos custava 88 chamadas e meio minuto só para desenhar, e por
dia navegado. Contar atendimento realizado, ao contrário, é número fechado e
local.

O `open` do documento continua aqui: o `document_url` espelhado aponta para o
app da vSaúde, que exigiria login de lá; o proxy resolve com a chave da
integração e grava a leitura na auditoria.
"""

from datetime import datetime, time, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.utils import timezone as dj_timezone
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api.permissions import IsPartnerArea
from apps.core.audit import log_action
from apps.core.context import resolve_active_membership
from apps.core.models.audit_log import AuditAction
from apps.integrations.ehr.exceptions import EHRError
from apps.integrations.ehr.registry import get_ehr_provider
from apps.patients.api.serializers.patient import PatientReadSerializer
from apps.patients.models import ClinicalEntry, ClinicalEntryKind
from apps.patients.partner_scope import pacientes_do_parceiro
from apps.scheduling.choices import AppointmentStatus
from apps.scheduling.models import Appointment

#: O que a área enxerga no prontuário. Nota e formulário são assunto da ficha
#: completa; aqui só sai o que o médico EMITIU.
KINDS_PARCEIROS = (ClinicalEntryKind.PRESCRIPTION, ClinicalEntryKind.EXAM)


def _dia(valor: str, campo: str):
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValidationError({campo: "Use a data no formato AAAA-MM-DD."}) from exc


def _fuso(clinic):
    """
    O dia da CLÍNICA, não o dia UTC: o atendimento das 21h pertence ao dia em
    que a recepção o viveu (mesma régua do `/appointments/summary/`).
    """
    try:
        return ZoneInfo(clinic.timezone)
    except Exception:
        return dj_timezone.get_default_timezone()


class PartnersDayView(APIView):
    """`GET /partners/day/?date=AAAA-MM-DD[&practitioner=]` - o dia da tela."""

    permission_classes = [IsPartnerArea]
    partner_allowed = True

    def get(self, request):
        clinic = resolve_active_membership(request).clinic

        dia = _dia(request.query_params.get("date"), "date")
        tz = _fuso(clinic)
        de = datetime.combine(dia, time.min, tzinfo=tz)
        ate = datetime.combine(dia + timedelta(days=1), time.min, tzinfo=tz)

        atendimentos = (
            Appointment.objects.filter(
                clinic=clinic,
                status=AppointmentStatus.COMPLETED,
                starts_at__gte=de,
                starts_at__lt=ate,
                patient__isnull=False,
            )
            .select_related("patient")
            .prefetch_related("patient__patient_tags__tag")
            .order_by("starts_at")
        )
        profissional = request.query_params.get("practitioner")
        if profissional:
            atendimentos = atendimentos.filter(practitioner_id=profissional)

        # A LISTA é de pacientes (a linha é a da tela de Pacientes), mas o
        # contador é de ATENDIMENTOS: quem foi atendido duas vezes no mesmo
        # dia é uma linha só e dois atendimentos.
        pacientes = {}
        total = 0
        for atendimento in atendimentos:
            total += 1
            pacientes.setdefault(atendimento.patient_id, atendimento.patient)

        serializer = PatientReadSerializer(
            list(pacientes.values()),
            many=True,
            context={"request": request, "clinic": clinic},
        )
        return Response(
            {
                "date": dia.isoformat(),
                "kpis": {"attendances": total, "patients": len(pacientes)},
                "patients": serializer.data,
            }
        )


class PartnersCalendarView(APIView):
    """
    `GET /partners/calendar/?year=&month=[&practitioner=]` - quantos
    atendimentos realizados por dia do mês.

    Existe para o calendário mostrar onde teve movimento antes de a pessoa
    clicar. Agregado no banco, como o `/appointments/summary/`.
    """

    permission_classes = [IsPartnerArea]
    partner_allowed = True

    def get(self, request):
        clinic = resolve_active_membership(request).clinic
        try:
            ano = int(request.query_params.get("year"))
            mes = int(request.query_params.get("month"))
            primeiro = datetime(ano, mes, 1).date()
        except (TypeError, ValueError) as exc:
            raise ValidationError({"month": "Informe ano e mês válidos."}) from exc

        tz = _fuso(clinic)
        de = datetime.combine(primeiro, time.min, tzinfo=tz)
        # Dezembro fecha em 1º de janeiro do ano SEGUINTE - o mês 13 não existe.
        seguinte = datetime(ano + (mes // 12), (mes % 12) + 1, 1).date()
        ate = datetime.combine(seguinte, time.min, tzinfo=tz)

        atendimentos = Appointment.objects.filter(
            clinic=clinic,
            status=AppointmentStatus.COMPLETED,
            starts_at__gte=de,
            starts_at__lt=ate,
            patient__isnull=False,
        )
        profissional = request.query_params.get("practitioner")
        if profissional:
            atendimentos = atendimentos.filter(practitioner_id=profissional)

        # `order_by()` limpa a ordenação padrão: sem isso o `starts_at` entra
        # no GROUP BY e a contagem sai fragmentada, uma linha por atendimento.
        por_dia = {
            str(linha["dia"].day): linha["total"]
            for linha in atendimentos.order_by()
            .annotate(dia=TruncDate("starts_at", tzinfo=tz))
            .values("dia")
            .annotate(total=Count("id"))
            .order_by("dia")
        }
        return Response({"year": ano, "month": mes, "by_day": por_dia})


class PartnerDocumentOpenView(APIView):
    """
    `GET /partners/documents/{id}/open/` - o PDF da receita ou do pedido.

    O `document_url` espelhado aponta para o app da vSaúde e exigiria login de
    lá; aqui o documento é buscado com a chave da integração. Buscar é barato,
    ENTREGAR é leitura de conteúdo clínico: audita antes, e o log responde
    "quem abriu o quê" sem guardar o arquivo.
    """

    permission_classes = [IsPartnerArea]
    partner_allowed = True

    def get(self, request, pk):
        membership = resolve_active_membership(request)
        clinic = membership.clinic

        entrada = (
            ClinicalEntry.objects.filter(
                clinic=clinic,
                pk=pk,
                kind__in=KINDS_PARCEIROS,
                # Mesmo escopo da ficha: sem isto o id na URL abria o PDF de
                # qualquer receita da clínica, inclusive de quem a tela nunca
                # mostra.
                patient__in=pacientes_do_parceiro(clinic),
            )
            .select_related("patient")
            .first()
        )
        if entrada is None:
            raise NotFound("Registro não encontrado.")
        documento_id = self._id_do_documento(entrada.document_url)
        if not documento_id:
            raise ValidationError(
                {"detail": "Este registro não tem documento para abrir."}
            )

        try:
            conteudo, mime = get_ehr_provider(clinic).export_document(documento_id)
        except EHRError as exc:
            raise ValidationError({"detail": str(exc)}) from exc

        log_action(
            user=request.user,
            action=AuditAction.READ,
            resource="ClinicalDocument",
            resource_id=entrada.pk,
            payload={
                "patient": entrada.patient_id,
                "kind": entrada.kind,
                "role": membership.role,
            },
            request=request,
            clinic=clinic,
        )

        tipo = "receita" if entrada.kind == ClinicalEntryKind.PRESCRIPTION else "pedido"
        nome = f"{tipo}-{entrada.date:%Y-%m-%d-%H%M}.pdf"
        resposta = HttpResponse(conteudo, content_type=mime)
        resposta["Content-Disposition"] = f'inline; filename="{nome}"'
        return resposta

    @staticmethod
    def _id_do_documento(document_url: str) -> str:
        """
        O guid do `?id=` do link espelhado. O link aponta para o app da
        vSaúde, mas o MESMO id responde na API pública com a nossa chave -
        calibrado em 30/07/2026.
        """
        if not document_url:
            return ""
        try:
            query = parse_qs(urlparse(document_url).query)
        except ValueError:
            return ""
        return (query.get("id") or [""])[0]
