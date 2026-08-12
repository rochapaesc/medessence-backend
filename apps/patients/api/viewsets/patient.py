from django.db.models import Count, Prefetch, Q
from rest_framework.decorators import action
from rest_framework.response import Response

from apps.core.api.guards import EHRDataGuardMixin
from apps.core.api.permissions import IsClinicManager
from apps.core.api.viewsets import ClinicScopedModelViewSet
from apps.core.audit import log_action
from apps.core.masking import is_masked
from apps.core.mixins import AuditMixin, SoftDeleteMixin
from apps.core.models.audit_log import AuditAction
from apps.patients.api.filtersets import PatientFilterset
from apps.patients.api.serializers import (
    PatientDetailSerializer,
    PatientReadSerializer,
    PatientWriteSerializer,
)
from apps.patients.api.viewsets.patient_files import PatientFilesMixin
from apps.patients.models import Patient, PatientTag, Tag
from apps.patients.partner_scope import eh_parceiro, pacientes_do_parceiro


class PatientViewSet(
    PatientFilesMixin,
    EHRDataGuardMixin,
    AuditMixin,
    SoftDeleteMixin,
    ClinicScopedModelViewSet,
):
    """
    CRM de pacientes (RF-PAC-1..7), escopado pela clínica ativa.
    Busca server-side (?search=) e filtros por tag/cidade/status/profissional.
    """

    #: O parceiro (RF-PAR-6) abre a ficha de UM paciente pela área dele, mas
    #: NÃO tem a listagem: `list` daria a carteira inteira a um usuário
    #: externo. Por action, e não pela view toda.
    partner_allowed = {"retrieve"}

    model = Patient
    audit_resource = "Patient"
    filterset_class = PatientFilterset
    serializer_class = PatientReadSerializer
    restore_permission_classes = [IsClinicManager]
    ordering_fields = ["name", "last_appointment_at", "created_at"]

    action_serializer_classes = {
        "list": PatientReadSerializer,
        "retrieve": PatientDetailSerializer,
        "create": PatientWriteSerializer,
        "update": PatientWriteSerializer,
        "partial_update": PatientWriteSerializer,
    }

    def get_queryset(self):
        queryset = (
            super()
            .get_queryset()
            .prefetch_related(
                Prefetch(
                    "patient_tags",
                    # Ordem de INSERÇÃO (created_at) — como o usuário adicionou.
                    queryset=PatientTag.objects.select_related("tag").order_by(
                        "created_at", "id"
                    ),
                )
            )
            .order_by("name")
        )
        if eh_parceiro(self.membership):
            # A permissão já limita o parceiro ao `retrieve`; sem ISTO ele
            # abria a ficha de QUALQUER paciente trocando o id na URL, com
            # CPF e endereço, mesmo de quem a tela dele nunca mostra.
            queryset = queryset.filter(pk__in=pacientes_do_parceiro(self.clinic))
        return queryset

    def retrieve(self, request, *args, **kwargs):
        """
        A ficha é o único lugar que revela o documento (§15) - cada acesso
        vira um `READ_CPF` no log, para responder "quem viu o CPF de quem".

        O gatilho é o que SAIU na resposta, não o papel de quem pediu: se um
        dia a regra mudar, a auditoria continua contando a verdade. O valor do
        documento não entra no payload do log.
        """
        response = super().retrieve(request, *args, **kwargs)

        cpf = (response.data or {}).get("cpf") or ""
        if cpf and not is_masked(cpf):
            membership = getattr(request, "active_membership", None)
            log_action(
                user=request.user,
                action=AuditAction.READ_CPF,
                resource=self.get_audit_resource(),
                resource_id=response.data.get("id", ""),
                payload={
                    "field": "cpf",
                    "role": getattr(membership, "role", "") or "",
                },
                request=request,
                clinic=self.get_audit_clinic(),
            )
        return response

    # ----------------- write-through p/ o EHR (§10.2) ----------------- #
    # Padrão único: salva local primeiro; com EHR configurado, enfileira
    # SyncOperation e o selo vira "aguardando sincronização". Standalone:
    # o fluxo termina aqui.

    def _enqueue_patient_push(self, patient, payload: dict) -> None:
        from apps.core.choices import SyncStatus
        from apps.integrations.push import enqueue_push

        operation = enqueue_push(self.clinic, "patient", patient.pk, payload)
        if operation is not None and patient.sync_status != SyncStatus.PENDING:
            patient.sync_status = SyncStatus.PENDING
            patient.save(update_fields=["sync_status", "updated_at"])

    def _enqueue_tags_push(self, patient) -> None:
        from apps.integrations.push import enqueue_push

        enqueue_push(self.clinic, "patient_tags", patient.pk, {"op": "set"})

    def perform_create(self, serializer):
        super().perform_create(serializer)
        patient = serializer.instance
        self._enqueue_patient_push(patient, {"op": "create"})
        if "tag_ids" in self.request.data:
            self._enqueue_tags_push(patient)

    def perform_update(self, serializer):
        super().perform_update(serializer)
        patient = serializer.instance
        self._enqueue_patient_push(patient, {"op": "update"})
        if "tag_ids" in self.request.data:
            self._enqueue_tags_push(patient)

    def perform_destroy(self, instance):
        external_id = instance.external_id
        super().perform_destroy(instance)  # soft delete + auditoria
        if external_id:
            from apps.integrations.push import enqueue_push

            enqueue_push(
                self.clinic,
                "patient",
                instance.pk,
                {"op": "delete", "external_id": external_id},
            )

    @action(detail=False, methods=["get"], url_path="counters")
    def counters(self, request):
        """
        Contadores por status (RF-PAC-2) - endpoint dedicado (RNF-5).

        Sem parâmetros: janela da clínica, carteira inteira.
        Com ?practitioner=<id>: carteira do profissional (pacientes que já
        consultaram com ele), na janela efetiva dele.
        """
        from rest_framework.exceptions import ValidationError

        from apps.patients.api.windows import parse_window
        from apps.scheduling.models import Practitioner

        queryset = self.get_queryset()
        override = parse_window(request)
        practitioner = None
        practitioner_id = request.query_params.get("practitioner")
        if practitioner_id:
            practitioner = Practitioner.objects.filter(
                clinic=self.clinic, pk=practitioner_id
            ).first()
            if practitioner is None:
                raise ValidationError({"practitioner": "Profissional não encontrado."})
            queryset = queryset.filter(appointments__practitioner=practitioner).distinct()
            window_days = practitioner.effective_active_window_days
        else:
            window_days = self.clinic.active_window_days
        if override:
            window_days = override

        return Response(queryset.status_counters(window_days, practitioner))

    @action(detail=False, methods=["get"], url_path="reactivation-summary")
    def reactivation_summary(self, request):
        """
        Contagens da fila de resgate, facetadas (RF-REA-1.4).

        Cada dimensão conta IGNORANDO o próprio filtro e respeitando os
        outros: escolher duas etiquetas muda os números das faixas, e escolher
        uma faixa muda os números das etiquetas. `total` é o recorte completo,
        com tudo aplicado - é o número que a faixa de contagem exibe.

        ⚠️ O recorte sai do MESMO filterset que serve a listagem, de
        propósito. Recalcular o segmento aqui à mão é como o contador e a
        lista divergiram na tela antiga: dois lugares definindo "quem está na
        fila" acabam definindo coisas diferentes.
        """
        from apps.patients.choices import Gender
        from apps.patients.models.patient import ABSENCE_RANGES

        def recorte(**overrides):
            params = request.query_params.copy()
            # ⚠️ NÃO força mais o segmento de resgate. A tela passou a
            # partir da base inteira (11/08/2026) e recorta pelos filtros
            # da coluna; quem quiser só a fila manda `?segment=` como
            # qualquer outro filtro.
            for chave, valor in overrides.items():
                if valor is None:
                    params.pop(chave, None)
                else:
                    params[chave] = valor
            return PatientFilterset(
                params, queryset=self.get_queryset(), request=request
            ).qs

        sem_faixa = recorte(absence=None)
        sem_etiqueta = recorte(tag=None)

        # "all" é o chip Todos: conceitualmente a ausência de faixa, e o front
        # não deveria ter que somar as outras três para desenhá-lo (somar no
        # cliente é como o número do topo passa a divergir da lista).
        por_faixa = [{"key": "all", "count": sem_faixa.count()}] + [
            {"key": faixa, "count": sem_faixa.by_absence(faixa).count()}
            for faixa in ABSENCE_RANGES
        ]
        por_etiqueta = [
            {
                "id": linha["patient_tags__tag_id"],
                "name": linha["patient_tags__tag__name"],
                "count": linha["count"],
            }
            for linha in (
                # ⚠️ `patient_tags__isnull=False` é obrigatório junto do
                # `deleted_at__isnull=True`: sozinho, o segundo passa também
                # para quem NÃO tem etiqueta nenhuma, porque o LEFT JOIN
                # produz NULL e `NULL IS NULL` é verdadeiro. Sem isto, o menu
                # de etiquetas ganha uma linha sem nome contando os sem tag.
                sem_etiqueta.filter(
                    patient_tags__isnull=False,
                    patient_tags__deleted_at__isnull=True,
                )
                .order_by()  # limpa ordenação p/ agregação
                .values("patient_tags__tag_id", "patient_tags__tag__name")
                .annotate(count=Count("id", distinct=True))
                .order_by("-count", "patient_tags__tag__name")
            )
        ]

        # O gênero são três valores fechados, então não precisa de catálogo
        # com busca: vem inteiro aqui, com zero onde não houver ninguém.
        sem_genero = recorte(gender=None)
        contagem_genero = {
            linha["gender"]: linha["n"]
            for linha in (
                sem_genero.order_by()
                .values("gender")
                .annotate(n=Count("id", distinct=True))
            )
        }
        por_genero = [
            {
                "key": opcao,
                "label": rotulo,
                "count": contagem_genero.get(opcao, 0),
            }
            for opcao, rotulo in Gender.choices
        ]

        # Ativo e inativo, com a mesma regra de faceta: ignoram o próprio
        # filtro. São dois valores calculados (e não uma coluna), então saem do
        # `by_status` do queryset em vez de um GROUP BY.
        from apps.patients.choices import PatientStatus

        sem_status = recorte(status=None)
        janela = self.clinic.active_window_days
        por_status = [
            {
                "key": opcao.value,
                "label": rotulo,
                "count": sem_status.by_status(opcao, janela).count(),
            }
            for opcao, rotulo in [
                (PatientStatus.ACTIVE, "Ativo"),
                (PatientStatus.INACTIVE, "Inativo"),
            ]
        ]

        # Profissional: quem já atendeu cada paciente. Sai por GROUP BY na
        # agenda, e não pelo `last_practitioner`, porque o filtro é "já
        # consultou com" e não "a última consulta foi com".
        from apps.scheduling.models import Practitioner

        sem_profissional = recorte(practitioner=None)
        contagem_prof = {
            linha["appointments__practitioner_id"]: linha["n"]
            for linha in (
                sem_profissional.filter(
                    appointments__isnull=False,
                    appointments__deleted_at__isnull=True,
                )
                .order_by()
                .values("appointments__practitioner_id")
                .annotate(n=Count("id", distinct=True))
            )
        }
        por_profissional = [
            {
                "id": profissional.pk,
                "name": profissional.name,
                "count": contagem_prof.get(profissional.pk, 0),
            }
            for profissional in Practitioner.objects.filter(
                clinic=self.clinic, deleted_at__isnull=True
            ).order_by("name")
        ]
        por_profissional.sort(key=lambda item: (-item["count"], item["name"]))

        return Response(
            {
                "total": recorte().count(),
                "by_absence": por_faixa,
                "by_tag": por_etiqueta,
                "by_gender": por_genero,
                "by_status": por_status,
                "by_practitioner": por_profissional,
            }
        )

    @action(detail=False, methods=["get"], url_path="rescue-tags")
    def rescue_tags(self, request):
        """
        O CATÁLOGO de etiquetas da fila, com a contagem do recorte ativo.

        ⚠️ Catálogo e contagem são coisas DIFERENTES, e confundi-las foi o
        defeito da primeira versão desta tela: a lista vinha facetada, então
        aplicar a faixa "3 a 6 meses" fazia 20 das 57 etiquetas sumirem e
        ficarem inalcançáveis - inclusive `COLONIA DO PIAUI`, que a recepção
        precisava justamente para ligar. Aqui a LISTA é sempre a da fila
        inteira; o que muda com o recorte é o NÚMERO ao lado, que pode ser 0.

        `?search=` filtra no servidor, como o resto do sistema. `?absence=`
        governa só a contagem. As etiquetas já escolhidas NÃO entram na conta,
        pela mesma regra de faceta do `reactivation-summary`: uma dimensão não
        conta a si mesma, senão marcar OEIRAS zeraria todas as outras.
        """
        from apps.patients.api.filtersets import PatientFilterset
        from apps.patients.choices import Gender
        from apps.patients.models.patient import ABSENCE_RANGES

        def recorte(**overrides):
            params = request.query_params.copy()
            # ⚠️ NÃO força mais o segmento de resgate. A tela passou a
            # partir da base inteira (11/08/2026) e recorta pelos filtros
            # da coluna; quem quiser só a fila manda `?segment=` como
            # qualquer outro filtro.
            params.pop("tag", None)  # faceta: a dimensão não conta a si mesma
            # ⚠️ Aqui `search` é o nome da ETIQUETA, e no filterset é o nome do
            # PACIENTE. Deixá-lo passar fazia a fila filtrar por gente chamada
            # "colonia", esvaziar, e o catálogo voltar vazio para toda busca.
            params.pop("search", None)
            for chave, valor in overrides.items():
                if valor is None:
                    params.pop(chave, None)
                else:
                    params[chave] = valor
            return PatientFilterset(
                params, queryset=self.get_queryset(), request=request
            ).qs

        fila_inteira = recorte(absence=None)
        no_recorte = recorte()

        busca = (request.query_params.get("search") or "").strip()
        catalogo = (
            Tag.objects.filter(
                clinic=self.clinic,
                deleted_at__isnull=True,
                # Na Tag o reverso chama `assignments`; `patient_tags` é o
                # nome do lado do Patient.
                assignments__deleted_at__isnull=True,
                assignments__patient__in=fila_inteira,
            )
            .distinct()
            .order_by("name")
        )
        if busca:
            catalogo = catalogo.filter(name__icontains=busca)

        # Uma query para as contagens, e o que não aparecer nela vale zero.
        contagens = {
            linha["patient_tags__tag_id"]: linha["n"]
            for linha in (
                no_recorte.filter(
                    patient_tags__isnull=False,
                    patient_tags__deleted_at__isnull=True,
                )
                .order_by()
                .values("patient_tags__tag_id")
                .annotate(n=Count("id", distinct=True))
            )
        }

        itens = [
            {"id": tag.pk, "name": tag.name, "count": contagens.get(tag.pk, 0)}
            for tag in catalogo
        ]
        # Quem tem gente no recorte primeiro; o resto em ordem alfabética,
        # visível e escolhível.
        itens.sort(key=lambda item: (-item["count"], item["name"]))

        return Response(
            {
                "count": len(itens),
                "absence": request.query_params.get("absence") or "all",
                "results": itens,
            }
        )

    @action(detail=False, methods=["get"], url_path="rescue-cities")
    def rescue_cities(self, request):
        """
        O catálogo de cidades da fila, com a contagem do recorte (RF-REA-1.8).

        Mesma regra do `rescue-tags`: a LISTA é a da fila inteira e o NÚMERO é
        do recorte, podendo ser zero. `?search=` busca no servidor.

        ⚠️ O texto vai CRU, sem normalizar caixa nem acento. `São Raimundo
        Nonato` e `SAO RAIMUNDO NONATO PIAUI` aparecem como duas entradas
        porque é assim que estão no cadastro. Juntar por regra automática
        mutila nome de verdade - a tentativa aqui virou `São João do`, comendo
        o "Piauí" que é parte do nome da cidade.
        """
        from apps.patients.api.filtersets import PatientFilterset

        def recorte(**overrides):
            params = request.query_params.copy()
            # ⚠️ NÃO força mais o segmento de resgate. A tela passou a
            # partir da base inteira (11/08/2026) e recorta pelos filtros
            # da coluna; quem quiser só a fila manda `?segment=` como
            # qualquer outro filtro.
            params.pop("city", None)  # faceta: a dimensão não conta a si mesma
            params.pop("search", None)  # aqui `search` é o nome da CIDADE
            for chave, valor in overrides.items():
                if valor is None:
                    params.pop(chave, None)
                else:
                    params[chave] = valor
            return PatientFilterset(
                params, queryset=self.get_queryset(), request=request
            ).qs

        busca = (request.query_params.get("search") or "").strip()

        def contar(queryset):
            return {
                linha["city"]: linha["n"]
                for linha in (
                    queryset.exclude(city="")
                    .order_by()
                    .values("city")
                    .annotate(n=Count("id", distinct=True))
                )
            }

        na_fila = contar(recorte(absence=None))
        no_recorte = contar(recorte())

        itens = [
            {"name": cidade, "count": no_recorte.get(cidade, 0)}
            for cidade in na_fila
            if not busca or busca.lower() in cidade.lower()
        ]
        itens.sort(key=lambda item: (-item["count"], item["name"]))

        return Response({"count": len(itens), "results": itens})

    @action(detail=False, methods=["get"], url_path="distribution")
    def distribution(self, request):
        """
        Agregações reais para o dashboard (RF-DSH): pacientes por tag e por
        cidade (os 6 maiores de cada). Cada item traz `count` (carteira toda)
        e `active_count` (só ativos na janela da clínica) - o front alterna
        entre "Todos" e "Ativos". Read-only, escopo da clínica ativa.

        Nota: acquisição ("novos no mês") não é derivável de `created_at` -
        na base sincronizada do EHR ele reflete a data de importação, não o
        cadastro real. Fica de fora até haver um campo de origem confiável.
        """
        from django.utils import timezone

        from apps.patients.api.windows import parse_window
        from apps.patients.models.patient import active_cutoff

        base = self.get_queryset().order_by()  # limpa ordenação p/ agregação
        window_days = parse_window(request) or self.clinic.active_window_days
        now = timezone.now()
        cutoff = active_cutoff(window_days, now)
        alive_assignment = Q(assignments__deleted_at__isnull=True) & Q(
            assignments__patient__deleted_at__isnull=True
        )
        # Ativo = compareceu na janela OU tem consulta futura agendada.
        active_patient = Q(assignments__patient__last_appointment_at__gte=cutoff) | Q(
            assignments__patient__next_appointment_at__gte=now
        )

        # Pacientes (vivos) por tag da clínica - total e ativos.
        tags = (
            Tag.objects.filter(clinic=self.clinic, deleted_at__isnull=True)
            .annotate(
                patients=Count("assignments__patient", filter=alive_assignment, distinct=True),
                active=Count(
                    "assignments__patient",
                    filter=alive_assignment & active_patient,
                    distinct=True,
                ),
            )
            .filter(patients__gt=0)
            .order_by("-patients", "name")
        )
        by_tag = [
            {
                "name": t.name,
                "color": t.color,
                "count": t.patients,
                "active_count": t.active,
            }
            for t in tags[:6]
        ]

        # Pacientes por cidade (ignora cidade vazia) - total e ativos.
        cities = (
            base.exclude(city="")
            .values("city")
            .annotate(
                count=Count("id"),
                active=Count(
                    "id",
                    filter=Q(last_appointment_at__gte=cutoff) | Q(next_appointment_at__gte=now),
                ),
            )
            .order_by("-count", "city")
        )
        by_city = [
            {"city": c["city"], "count": c["count"], "active_count": c["active"]}
            for c in cities[:6]
        ]

        return Response({"by_tag": by_tag, "by_city": by_city})
