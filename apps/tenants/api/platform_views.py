"""
API do plano plataforma (§4.8, RF-ADM-1/3/4/6).

  GET/POST   /platform/clinics/              lista e cria (com o gestor junto)
  GET/PATCH  /platform/clinics/{id}/         detalhe e configuração
  POST       /platform/clinics/{id}/suspend/     tira do ar, com motivo
  POST       /platform/clinics/{id}/reactivate/  devolve ao ar
  GET        /platform/overview/             os números agregados
  GET        /platform/sync/                 sincronização por tenant

⚠️ Nada aqui é escopado por clínica: este é o único lugar do produto que
enxerga vários tenants. Por isso a permission é `IsPlatformAdmin` na classe E
o que sai é sempre contagem ou estado - nunca conteúdo (RF-ADM-6).

Até 21/08/2026 o papel `is_platform_admin` existia e era usado num único
lugar (a permissão do `restore`): não havia API nem tela, e os admins da
plataforma não têm vínculo com clínica nenhuma, então o app inteiro - que é
escopado por clínica - simplesmente não os atendia.
"""

from datetime import timedelta

from django.db.models import Count, OuterRef, Q, Subquery
from django.utils import timezone
from drf_spectacular.utils import extend_schema
from rest_framework.decorators import action
from rest_framework.mixins import (
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
)
from rest_framework.response import Response
from rest_framework.views import APIView
from rest_framework.viewsets import GenericViewSet

from apps.core.api.permissions import IsPlatformAdmin
from apps.tenants.api.platform_serializers import (
    ClinicSuspendSerializer,
    PlatformClinicCreateSerializer,
    PlatformClinicSerializer,
    PlatformClinicUpdateSerializer,
)
from apps.tenants.api.platform_serializers import PlatformClinicDetailSerializer
from apps.tenants.choices import ClinicStatus
from apps.tenants.models import Clinic
from apps.tenants.platform import create_clinic, reactivate_clinic, suspend_clinic

# Janela dos números de movimento. Trinta dias é o que responde "esta clínica
# está VIVA?" - o total desde sempre não distingue quem parou ontem de quem
# nunca começou.
JANELA_DIAS = 30


def _desde():
    return timezone.now() - timedelta(days=JANELA_DIAS)


@extend_schema(tags=["platform"])
class PlatformClinicViewSet(
    CreateModelMixin,
    ListModelMixin,
    RetrieveModelMixin,
    UpdateModelMixin,
    GenericViewSet,
):
    """
    As clínicas da plataforma (RF-ADM-1).

    Sem `destroy` de propósito (RF-ADM-1.6): suspender é reversível, apagar um
    tenant inteiro não é. Quem precisar disso vai ao admin do Django, onde o
    ato é deliberado.
    """

    permission_classes = [IsPlatformAdmin]
    serializer_class = PlatformClinicSerializer
    http_method_names = ["get", "post", "patch", "options", "head"]

    def get_serializer_class(self):
        if self.action == "create":
            return PlatformClinicCreateSerializer
        if self.action in ("update", "partial_update"):
            return PlatformClinicUpdateSerializer
        if self.action == "retrieve":
            # O detalhe carrega os seis cartões (RF-ADM-1.8); a lista fica
            # leve, sem as consultas por objeto que só o detalhe justifica.
            return PlatformClinicDetailSerializer
        return PlatformClinicSerializer

    def get_queryset(self):
        # ⚠️ Filtro é SERVER-SIDE (regra do usuário, 21/08): o front não
        # peneira lista. `get_object` NÃO passa por aqui de propósito: o
        # detalhe de uma clínica fora do filtro atual continua alcançável.
        return self._consulta(
            busca=self.request.query_params.get("search", "").strip(),
            situacao=self.request.query_params.get("status", "").strip(),
        )

    def _consulta(self, busca="", situacao=""):
        from apps.inbox.models import Channel, Message

        desde = _desde()
        canal = (
            Channel.objects.filter(clinic=OuterRef("pk"), deleted_at__isnull=True, is_test=False)
            .order_by("-connected_at", "-id")
            .values("pk")[:1]
        )
        base = Clinic.objects.all()
        if busca:
            from django.db.models import Q as _Q

            base = base.filter(_Q(name__icontains=busca) | _Q(slug__icontains=busca))
        if situacao in (ClinicStatus.ACTIVE, ClinicStatus.SUSPENDED):
            base = base.filter(status=situacao)
        consulta = (
            base
            .annotate(
                members_count=Count(
                    "memberships",
                    filter=Q(memberships__is_active=True, memberships__deleted_at__isnull=True),
                    distinct=True,
                ),
                patients_count=Count(
                    "patients",
                    filter=Q(patients__deleted_at__isnull=True),
                    distinct=True,
                ),
                conversations_30d=Count(
                    "conversations",
                    filter=Q(
                        conversations__deleted_at__isnull=True,
                        conversations__last_message_at__gte=desde,
                    ),
                    distinct=True,
                ),
                canal_id=Subquery(canal),
            )
            .order_by("name")
        )

        # ⚠️ As mensagens saem de uma consulta À PARTE, e não de mais um
        # `Count` no mesmo queryset: somar vários JOINs de tabelas grandes na
        # mesma linha multiplica o custo, e Message é a maior do banco.
        contagem = dict(
            Message.objects.filter(created_at__gte=desde, deleted_at__isnull=True)
            .values_list("clinic_id")
            .annotate(total=Count("pk"))
        )
        canais = {
            c.pk: c
            for c in Channel.objects.filter(
                pk__in=[c.canal_id for c in consulta if c.canal_id]
            )
        }
        # "Está viva?" (RF-ADM-4.3): a última mensagem de cada clínica, numa
        # consulta só, pelo mesmo motivo da contagem acima.
        from django.db.models import Max

        ultima_msg = dict(
            Message.objects.filter(deleted_at__isnull=True)
            .values_list("clinic_id")
            .annotate(ultima=Max("created_at"))
        )
        for clinica in consulta:
            clinica.messages_30d = contagem.get(clinica.pk, 0)
            clinica.canal_ativo = canais.get(clinica.canal_id)
            clinica.last_message_at = ultima_msg.get(clinica.pk)
        return consulta

    def get_object(self):
        # O queryset acima é resolvido em lista (as contagens fora do SQL), e
        # `get_object` precisa da instância anotada para o serializer. SEM os
        # filtros da lista: o detalhe não some porque a busca mudou.
        pk = self.kwargs["pk"]
        for clinica in self._consulta():
            if str(clinica.pk) == str(pk):
                return clinica
        from django.http import Http404

        raise Http404

    def _corpo(self, clinic_id):
        return PlatformClinicSerializer(
            next(c for c in self._consulta() if c.pk == clinic_id)
        ).data

    def create(self, request, *args, **kwargs):
        serializer = self.get_serializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        clinic, senha = create_clinic(
            actor=request.user,
            name=serializer.validated_data["name"],
            slug=serializer.validated_data["slug"],
            timezone_name=serializer.validated_data.get("timezone", "America/Fortaleza"),
            manager_name=serializer.validated_data["manager_name"],
            manager_email=serializer.validated_data["manager_email"],
            request=request,
        )

        corpo = self._corpo(clinic.pk)
        # ⚠️ A senha temporária viaja UMA vez, aqui, e não é gravada em lugar
        # nenhum (RF-EQP-2.2). Vem nula quando o e-mail já tinha conta: nesse
        # caso o gestor entra com a senha que já usa.
        corpo["manager_temporary_password"] = senha
        return Response(corpo, status=201)

    def update(self, request, *args, **kwargs):
        clinic = self.get_object()
        serializer = self.get_serializer(clinic, data=request.data, partial=True)
        serializer.is_valid(raise_exception=True)
        serializer.save()
        return Response(self._corpo(clinic.pk))

    @action(detail=True, methods=["post"], url_path="suspend")
    def suspend(self, request, pk=None):
        """Tira a clínica do ar (RF-ADM-1.4). O motivo é obrigatório."""
        clinic = self.get_object()
        serializer = ClinicSuspendSerializer(data=request.data)
        serializer.is_valid(raise_exception=True)

        suspend_clinic(
            clinic,
            actor=request.user,
            category=serializer.validated_data["category"],
            reason=serializer.validated_data["reason"],
            request=request,
        )
        return Response(self._corpo(clinic.pk))

    @action(detail=True, methods=["post"], url_path="reactivate")
    def reactivate(self, request, pk=None):
        """Devolve a clínica ao ar (RF-ADM-1.5)."""
        clinic = self.get_object()
        reactivate_clinic(clinic, actor=request.user, request=request)
        return Response(self._corpo(clinic.pk))


@extend_schema(tags=["platform"])
class PlatformOverviewView(APIView):
    """
    Os números agregados da plataforma (RF-ADM-4).

    ⚠️ Contagens e nada mais: sem lista nominal de pacientes, sem conteúdo de
    conversa ou prontuário. A regra está no §4.8 e vale como cerca de código.
    """

    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        from apps.accounts.models import User
        from apps.inbox.models import Conversation, Message
        from apps.patients.models import Patient

        desde = _desde()
        por_status = dict(
            Clinic.objects.values_list("status").annotate(total=Count("pk"))
        )
        ativas = por_status.get(ClinicStatus.ACTIVE, 0)
        suspensas = por_status.get(ClinicStatus.SUSPENDED, 0)

        return Response(
            {
                "clinics": ativas + suspensas,
                "clinics_active": ativas,
                "clinics_suspended": suspensas,
                "users": User.objects.filter(is_active=True).count(),
                "patients": Patient.objects.count(),
                "conversations_30d": Conversation.objects.filter(
                    last_message_at__gte=desde
                ).count(),
                "messages_30d": Message.objects.filter(created_at__gte=desde).count(),
                "window_days": JANELA_DIAS,
                "messages_by_day": self._serie_por_dia(desde),
                "attention": self._atencao(),
            }
        )

    def _serie_por_dia(self, desde):
        """
        Mensagens por dia (RF-ADM-4.1): número sem tempo não diz se o produto
        cresce ou parou. Dias sem mensagem entram como zero, senão o gráfico
        encolhe o eixo e esconde justamente o buraco.
        """
        from django.db.models.functions import TruncDate

        from apps.inbox.models import Message

        por_dia = dict(
            Message.objects.filter(created_at__gte=desde, deleted_at__isnull=True)
            .annotate(dia=TruncDate("created_at"))
            .values_list("dia")
            .annotate(total=Count("pk"))
        )
        inicio = timezone.localdate(desde)
        hoje = timezone.localdate()
        serie = []
        dia = inicio
        while dia <= hoje:
            serie.append({"day": dia, "count": por_dia.get(dia, 0)})
            dia += timedelta(days=1)
        return serie

    def _atencao(self):
        """
        Os TRÊS tipos de problema num bloco só (RF-ADM-4.2): canal caído,
        sincronização falhando e mensagem presa. Nasceu porque só o EHR
        gerava aviso, e a clínica real ficou dias com o WhatsApp fora sem a
        Visão geral gritar.
        """
        from apps.inbox.models import Channel
        from apps.inbox.services import mensagens_para_reenviar
        from apps.integrations.models import SyncRun

        itens = []

        # Canal caído: a clínica não envia nem recebe. É o mais grave.
        for canal in (
            Channel.objects.filter(
                is_test=False,
                deleted_at__isnull=True,
                disconnected_at__isnull=False,
                clinic__deleted_at__isnull=True,
            ).select_related("clinic")
        ):
            itens.append(
                {
                    "kind": "channel_down",
                    "clinic": {"id": canal.clinic_id, "name": canal.clinic.name},
                    "detail": canal.disconnect_reason or "sem motivo registrado",
                    "since": canal.disconnected_at,
                }
            )

        # Sincronização falhando: a última execução de cada tipo, com erro,
        # de clínica ativa com prontuário.
        ultimas = {}
        for run in SyncRun.objects.filter(
            clinic__deleted_at__isnull=True,
            clinic__status=ClinicStatus.ACTIVE,
            clinic__ehr_provider__gt="",
        ).select_related("clinic").order_by("clinic_id", "kind", "-started_at", "-id"):
            ultimas.setdefault((run.clinic_id, run.kind), run)
        for run in ultimas.values():
            if not run.error:
                continue
            itens.append(
                {
                    "kind": "sync_failed",
                    "clinic": {"id": run.clinic_id, "name": run.clinic.name},
                    "detail": f"{run.get_kind_display()}: {run.error}",
                    "since": run.started_at,
                }
            )

        # Mensagens presas: saíram do CRM e nunca se confirmaram (a mesma
        # régua do inbox_doctor e da faixa do Inbox).
        for clinic_id, nome in (
            Channel.objects.filter(is_test=False, deleted_at__isnull=True)
            .values_list("clinic_id", "clinic__name")
            .distinct()
        ):
            presas = mensagens_para_reenviar(clinic_id).count()
            if not presas:
                continue
            itens.append(
                {
                    "kind": "stuck_messages",
                    "clinic": {"id": clinic_id, "name": nome},
                    "detail": (
                        f"{presas} mensagem(ns) não saíram nas últimas 24 horas"
                    ),
                    "count": presas,
                    "since": None,
                }
            )
        return itens


@extend_schema(tags=["platform"])
class PlatformSyncView(APIView):
    """
    Sincronização com o prontuário, por tenant (RF-ADM-3).

    ⚠️ Quem falhou ou está parada vem PRIMEIRO. Um painel que só lista em
    ordem alfabética não avisa nada: o erro fica na quinta linha e ninguém o
    vê até a clínica ligar reclamando que a agenda não atualiza.
    """

    permission_classes = [IsPlatformAdmin]

    # Acima disto a sincronização é considerada PARADA. O beat roda de hora em
    # hora; seis horas sem execução nenhuma não é atraso, é coisa quebrada.
    HORAS_ATE_PARADA = 6

    def get(self, request):
        from apps.integrations.models import SyncRun

        agora = timezone.now()
        limite = agora - timedelta(hours=self.HORAS_ATE_PARADA)

        ultimas = {}
        for run in SyncRun.objects.filter(
            clinic__deleted_at__isnull=True
        ).order_by("clinic_id", "kind", "-started_at", "-id"):
            ultimas.setdefault((run.clinic_id, run.kind), run)

        linhas = []
        for clinic in Clinic.objects.filter(deleted_at__isnull=True).order_by("name"):
            execucoes = [
                {
                    "kind": kind,
                    "kind_display": run.get_kind_display(),
                    "started_at": run.started_at,
                    "finished_at": run.finished_at,
                    "error": run.error or "",
                    "stats": run.stats or {},
                }
                for (clinic_id, kind), run in ultimas.items()
                if clinic_id == clinic.pk
            ]
            com_erro = sum(1 for e in execucoes if e["error"])
            recente = max(
                (e["finished_at"] or e["started_at"] for e in execucoes if e["started_at"]),
                default=None,
            )
            linhas.append(
                {
                    "clinic": {
                        "id": clinic.pk,
                        "name": clinic.name,
                        "status": clinic.status,
                    },
                    # Clínica sem prontuário não é problema de sync: ela nunca
                    # sincroniza, e marcá-la como parada encheria o painel de
                    # alarme falso.
                    "ehr_provider": clinic.ehr_provider,
                    "runs": sorted(execucoes, key=lambda e: e["kind"]),
                    "failures": com_erro,
                    "last_activity": recente,
                    "stalled": bool(
                        clinic.ehr_provider
                        and not clinic.is_suspended
                        and (recente is None or recente < limite)
                    ),
                }
            )

        # Problema primeiro: falhas, depois paradas, depois o resto por nome.
        linhas.sort(key=lambda linha: (-linha["failures"], not linha["stalled"]))
        return Response({"clinics": linhas, "stalled_after_hours": self.HORAS_ATE_PARADA})


@extend_schema(tags=["platform"])
class PlatformUsersView(APIView):
    """
    Pessoas (RF-ADM-7): toda conta do sistema numa lista só.

    Responde "quem tem acesso a quê" e "quem nunca entrou" sem abrir clínica
    por clínica. LEITURA nesta fase; desativar conta e dar ou tirar admin
    ficam para o eixo poder. Vínculo traz clínica e papel, que são CADASTRO -
    a cerca de conteúdo do RF-ADM-6 segue intacta.
    """

    permission_classes = [IsPlatformAdmin]

    def get(self, request):
        from django.db.models import Prefetch, Q

        from apps.accounts.models import User
        from apps.accounts.models.membership import Membership

        usuarios = User.objects.all()

        # Filtro SERVER-SIDE (regra do usuário, 21/08): busca por nome ou
        # e-mail, clínica do vínculo e tipo de usuário. `platform_admin` é
        # tipo aqui, ainda que no modelo seja flag e não papel.
        busca = request.query_params.get("search", "").strip()
        if busca:
            usuarios = usuarios.filter(
                Q(first_name__icontains=busca)
                | Q(last_name__icontains=busca)
                | Q(email__icontains=busca)
            )
        clinica = request.query_params.get("clinic", "").strip()
        if clinica.isdigit():
            usuarios = usuarios.filter(
                memberships__clinic_id=int(clinica),
                memberships__deleted_at__isnull=True,
            )
        papel = request.query_params.get("role", "").strip()
        if papel == "platform_admin":
            usuarios = usuarios.filter(is_platform_admin=True)
        elif papel:
            usuarios = usuarios.filter(
                memberships__role=papel,
                memberships__deleted_at__isnull=True,
            )

        usuarios = (
            usuarios.distinct()
            .prefetch_related(
                Prefetch(
                    "memberships",
                    queryset=Membership.objects.filter(
                        deleted_at__isnull=True
                    ).select_related("clinic"),
                )
            )
            .order_by("first_name", "last_name", "email")
        )
        return Response(
            {
                "users": [
                    {
                        "id": u.pk,
                        "name": u.get_full_name() or u.email,
                        "email": u.email,
                        "is_platform_admin": u.is_platform_admin,
                        "is_active": u.is_active,
                        "last_login": u.last_login,
                        "created_at": u.created_at,
                        "memberships": [
                            {
                                "clinic_id": m.clinic_id,
                                "clinic_name": m.clinic.name,
                                "role": m.role,
                                "role_display": m.get_role_display(),
                                "is_active": m.is_active,
                            }
                            for m in u.memberships.all()
                        ],
                    }
                    for u in usuarios
                ],
            }
        )


@extend_schema(tags=["platform"])
class PlatformHealthView(APIView):
    """
    Saúde do sistema (RF-ADM-8): o `inbox_doctor` com porta na tela.

    Reusa `apps.core.health` inteiro - worker, batimento, assinatura do
    código e filas - e soma o que o comando não tinha: banco, migrações
    pendentes e a LISTA das mensagens presas, identificadas por clínica e
    número da conversa, nunca pelo conteúdo.
    """

    permission_classes = [IsPlatformAdmin]

    # Presas listadas uma a uma até aqui; acima disto só a contagem importa.
    MAX_PRESAS_LISTADAS = 20

    def get(self, request):
        from apps.core.health import (
            assinatura_do_codigo,
            saude_do_processamento,
            tamanho_das_filas,
        )

        return Response(
            {
                "code_signature": assinatura_do_codigo(),
                "database": self._banco(),
                "pending_migrations": self._migracoes_pendentes(),
                "worker": saude_do_processamento(),
                "queues": tamanho_das_filas(),
                "stuck_messages": self._presas(),
            }
        )

    def _banco(self):
        from django.db import connection

        try:
            connection.ensure_connection()
            return {"alive": True}
        except Exception as exc:  # noqa: BLE001 - o painel reporta, não trata
            return {"alive": False, "reason": str(exc)}

    def _migracoes_pendentes(self):
        """
        O deploy que esquece o `migrate` quebra longe do deploy. O chatwoot
        mostra o mesmo aviso no Instance status, pelo mesmo motivo.
        """
        from django.db import connection
        from django.db.migrations.executor import MigrationExecutor

        try:
            executor = MigrationExecutor(connection)
            plano = executor.migration_plan(
                executor.loader.graph.leaf_nodes()
            )
            return len(plano)
        except Exception:  # noqa: BLE001
            return None

    def _presas(self):
        from apps.inbox.models import Channel
        from apps.inbox.services import mensagens_para_reenviar

        itens = []
        total = 0
        for clinic_id, nome in (
            Channel.objects.filter(is_test=False, deleted_at__isnull=True)
            .values_list("clinic_id", "clinic__name")
            .distinct()
        ):
            presas = mensagens_para_reenviar(clinic_id).order_by("-created_at")
            total += presas.count()
            for msg in presas[: self.MAX_PRESAS_LISTADAS]:
                if len(itens) >= self.MAX_PRESAS_LISTADAS:
                    break
                itens.append(
                    {
                        "clinic": {"id": clinic_id, "name": nome},
                        "conversation_id": msg.conversation_id,
                        "attempted_at": msg.send_attempted_at or msg.created_at,
                    }
                )
        return {"total": total, "items": itens}
