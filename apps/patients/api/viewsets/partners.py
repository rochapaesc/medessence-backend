"""
Área de Parceiros (RF-PAR, §4.11) - os pacientes do período com receita ou
pedido de exame, montados sobre o espelho clínico.

Duas decisões moram aqui e não são óbvias:

- **Quem entra é o gestor e o papel `partner`** (RF-PAR-6). O parceiro é um
  usuário EXTERNO: a permissão desta área é a única que o aceita - todo o
  resto da API o recusa pela cerca do `IsClinicMember`.
- **A tela dispara a conferência do espelho** (RF-PAR-4). O prontuário local
  só sincroniza ao abrir a ficha, então os atendidos de hoje quase nunca
  estão nele. Ao abrir um período, os pacientes COM CONSULTA nele entram numa
  conferência dirigida em background, e a resposta avisa que está buscando.
"""

from datetime import datetime, time, timedelta
from urllib.parse import parse_qs, urlparse
from zoneinfo import ZoneInfo

from django.db.models import Count
from django.db.models.functions import TruncDate
from django.http import HttpResponse
from django.utils import timezone as dj_timezone
from django.utils.html import strip_tags
from rest_framework.exceptions import NotFound, ValidationError
from rest_framework.response import Response
from rest_framework.views import APIView

from apps.core.api.permissions import IsPartnerArea
from apps.core.audit import log_action
from apps.core.context import resolve_active_membership
from apps.core.models.audit_log import AuditAction
from apps.integrations.choices import SyncRunKind
from apps.integrations.ehr.exceptions import EHRError
from apps.integrations.ehr.registry import get_ehr_provider
from apps.integrations.models import SyncRun
from apps.patients.models import ClinicalEntry, ClinicalEntryKind, Patient
from apps.scheduling.models import Appointment

#: Tipos que a área enxerga - nota e formulário são assunto da ficha.
KINDS_PARCEIROS = (ClinicalEntryKind.PRESCRIPTION, ClinicalEntryKind.EXAM)

#: Teto da conferência dirigida (RF-PAR-4): um mês cheio da clínica real tem
#: ~200 consultas; conferir os mais recentes primeiro cobre o que a tela
#: mostra sem segurar a fila de sync.
CONFERENCIA_MAXIMA = 60

#: Anti-rajada: se a última rodada mirou exatamente os que ainda faltam e
#: acabou há menos que isto, não dispara outra.
CONFERENCIA_INTERVALO = timedelta(minutes=5)


def _dia(valor: str, campo: str):
    try:
        return datetime.strptime(valor, "%Y-%m-%d").date()
    except (TypeError, ValueError) as exc:
        raise ValidationError({campo: "Use a data no formato AAAA-MM-DD."}) from exc


def _fuso(clinic):
    """
    O dia da CLÍNICA, não o dia UTC: a receita das 21h pertence ao dia em que
    a recepção a viveu (mesma régua do `/appointments/summary/`).
    """
    try:
        return ZoneInfo(clinic.timezone)
    except Exception:
        return dj_timezone.get_default_timezone()


class PartnersSummaryView(APIView):
    """`GET /partners/summary/?from=&to=[&practitioner=]` - o período da tela."""

    permission_classes = [IsPartnerArea]
    partner_allowed = True

    def get(self, request):
        membership = resolve_active_membership(request)
        clinic = membership.clinic

        inicio = _dia(request.query_params.get("from"), "from")
        fim = _dia(request.query_params.get("to"), "to")
        if fim < inicio:
            raise ValidationError({"to": "O fim do período vem antes do começo."})

        tz = _fuso(clinic)
        de = datetime.combine(inicio, time.min, tzinfo=tz)
        ate = datetime.combine(fim + timedelta(days=1), time.min, tzinfo=tz)

        entradas = (
            ClinicalEntry.objects.filter(
                clinic=clinic,
                kind__in=KINDS_PARCEIROS,
                date__gte=de,
                date__lt=ate,
                patient__isnull=False,
            )
            .select_related("patient", "practitioner")
            .order_by("date")
        )
        profissional = request.query_params.get("practitioner")
        if profissional:
            entradas = entradas.filter(practitioner_id=profissional)

        consultas = (
            Appointment.objects.filter(
                clinic=clinic,
                starts_at__gte=de,
                starts_at__lt=ate,
                patient__isnull=False,
            )
            .select_related("practitioner")
            .order_by("starts_at")
        )
        # A consulta é indexada por (paciente, DIA), não por paciente.
        # Indexada só por paciente, a linha do documento do dia 24 mostrava a
        # consulta do dia 14 - 30 documentos de julho saíam assim.
        consulta_do_dia = {}
        for consulta in consultas:
            chave = (consulta.patient_id, consulta.starts_at.astimezone(tz).date())
            consulta_do_dia.setdefault(chave, consulta)

        pacientes = {}
        receitas = 0
        exames = 0
        for entrada in entradas:
            linha = pacientes.setdefault(
                entrada.patient_id,
                {
                    "id": entrada.patient_id,
                    "name": entrada.patient.name,
                    "appointment": None,
                    "docs": [],
                },
            )
            if entrada.kind == ClinicalEntryKind.PRESCRIPTION:
                receitas += 1
            else:
                exames += 1
            linha["docs"].append(
                {
                    "id": entrada.pk,
                    "kind": entrada.kind,
                    "at": entrada.date.isoformat(),
                    "dia": entrada.date.astimezone(tz).date(),
                    "practitioner": (
                        entrada.practitioner.name
                        if entrada.practitioner
                        else entrada.creator_name
                    ),
                    # No pedido de exame a descrição diz O QUE foi solicitado
                    # ("SOLICITO eletrocardiograma...") - texto puro, curto.
                    "description": strip_tags(entrada.description or "")
                    .replace("\n", " ")
                    .strip()[:200],
                    "has_document": bool(entrada.document_url),
                }
            )
        for paciente_id, linha in pacientes.items():
            dias = {doc["dia"] for doc in linha["docs"]}
            # A consulta só aparece quando é INEQUÍVOCA: um dia de documento e
            # uma consulta naquele dia. Em Semana/Mês com documentos em dias
            # diferentes não existe "a consulta", e a linha mostra o intervalo
            # em vez de escolher uma e mentir.
            consulta = (
                consulta_do_dia.get((paciente_id, next(iter(dias))))
                if len(dias) == 1
                else None
            )
            if consulta is not None:
                linha["appointment"] = {
                    "at": consulta.starts_at.isoformat(),
                    "practitioner": (
                        consulta.practitioner.name if consulta.practitioner else ""
                    ),
                }
            linha["days"] = [dia.isoformat() for dia in sorted(dias)]
            for doc in linha["docs"]:
                doc.pop("dia", None)

        return Response(
            {
                "from": inicio.isoformat(),
                "to": fim.isoformat(),
                "kpis": {
                    "prescriptions": receitas,
                    "exams": exames,
                    "patients": len(pacientes),
                },
                "conference": self._conferir_espelho(clinic, consultas, pacientes),
                "patients": sorted(
                    pacientes.values(),
                    key=lambda linha: linha["docs"][0]["at"],
                ),
            }
        )

    def _conferir_espelho(self, clinic, consultas, pacientes) -> dict:
        """
        Dispara a conferência dirigida (RF-PAR-4) e devolve o quanto do período
        já foi coberto.

        ⚠️ Devolve COBERTURA, não um "estou buscando" - o booleano de antes
        virava falso depois de conferir 60 de 342 pacientes e a tela dizia que
        havia terminado. Número parcial com cara de final é pior do que número
        nenhum, ainda mais numa tela de acerto com parceiro.
        """
        vazio = {"running": False, "checked": 0, "total": 0, "complete": True}
        if not clinic.ehr_provider:
            return vazio

        # O universo do período: quem tem consulta MAIS quem já aparece com
        # documento (a renovação sem consulta também precisa ser reconferida).
        universo = list(
            dict.fromkeys(
                [c.patient_id for c in consultas] + list(pacientes.keys())
            )
        )
        if not universo:
            return vazio

        conferidos = set(
            Patient.objects.filter(
                pk__in=universo, clinical_synced_at__isnull=False
            ).values_list("pk", flat=True)
        )
        cobertura = {
            "checked": len(conferidos),
            "total": len(universo),
            "complete": len(conferidos) == len(universo),
        }

        rodadas = SyncRun.objects.filter(
            clinic=clinic, kind=SyncRunKind.MEDICAL_RECORDS
        )
        if rodadas.filter(finished_at__isnull=True).exists():
            return {"running": True, **cobertura}

        faltando = [pk for pk in universo if pk not in conferidos]
        if not faltando:
            return {"running": False, **cobertura}

        ultima = rodadas.order_by("-started_at").first()
        if (
            ultima
            and ultima.finished_at
            and dj_timezone.now() - ultima.finished_at < CONFERENCIA_INTERVALO
            and set(faltando) <= set(ultima.stats.get("patient_ids") or [])
        ):
            # A rodada recente já mirou exatamente estes: não redisparar.
            return {"running": False, **cobertura}

        from apps.integrations.tasks import sync_partner_records

        # Só os que FALTAM, em lotes: abrir de novo avança a fila em vez de
        # reconferir os mesmos 60 para sempre.
        sync_partner_records.delay(clinic.pk, faltando[:CONFERENCIA_MAXIMA])
        return {"running": True, **cobertura}


class PartnerDocumentOpenView(APIView):
    """
    `GET /partners/documents/{id}/open/` - o PDF da receita ou do pedido.

    É o par do `open` da aba Arquivos: buscar o documento no EHR é barato,
    mas ENTREGÁ-LO é leitura de conteúdo clínico - então audita antes, e o
    log responde "quem abriu o quê" sem guardar o arquivo.
    """

    permission_classes = [IsPartnerArea]
    partner_allowed = True

    def get(self, request, pk):
        membership = resolve_active_membership(request)
        clinic = membership.clinic

        entrada = (
            ClinicalEntry.objects.filter(
                clinic=clinic, pk=pk, kind__in=KINDS_PARCEIROS
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
        vSaúde (login deles), mas o MESMO id responde na API pública com a
        nossa chave - calibrado em 30/07/2026.
        """
        if not document_url:
            return ""
        try:
            query = parse_qs(urlparse(document_url).query)
        except ValueError:
            return ""
        return (query.get("id") or [""])[0]


class PartnersCalendarView(APIView):
    """
    `GET /partners/calendar/?year=&month=[&practitioner=]` - quantos documentos
    por dia do mês.

    Existe para o calendário da tela **mostrar onde tem coisa** antes de o
    usuário clicar: navegar às cegas obriga a abrir dia a dia para descobrir
    que a clínica não atendeu na terça. Agregado no banco, como o
    `/appointments/summary/` - a alternativa seria baixar o mês inteiro só
    para contar.
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
        seguinte = datetime(ano + (mes // 12), (mes % 12) + 1, 1).date()
        ate = datetime.combine(seguinte, time.min, tzinfo=tz)

        entradas = ClinicalEntry.objects.filter(
            clinic=clinic,
            kind__in=KINDS_PARCEIROS,
            date__gte=de,
            date__lt=ate,
            patient__isnull=False,
        )
        profissional = request.query_params.get("practitioner")
        if profissional:
            entradas = entradas.filter(practitioner_id=profissional)

        # `order_by()` limpa a ordenação padrão: sem isso o `date` entra no
        # GROUP BY e a contagem sai fragmentada (uma linha por documento).
        por_dia = {
            str(linha["dia"].day): linha["total"]
            for linha in entradas.order_by()
            .annotate(dia=TruncDate("date", tzinfo=tz))
            .values("dia")
            .annotate(total=Count("id"))
            .order_by("dia")
        }
        return Response({"year": ano, "month": mes, "by_day": por_dia})
