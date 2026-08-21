"""
As regras do plano plataforma (§4.8, RF-ADM-1).

Fora do viewset pelo mesmo motivo de `accounts/team.py`: a camada HTTP só
traduz erro, e quem confia na cerca da rota acaba com uma porta sem guarda no
dia em que outra rota chegar à mesma operação. Aqui já chegou uma: o
`manage.py clinica` cria clínica pelo terminal, e a criação da tela tem de
seguir a mesma regra.

⚠️ Este módulo NÃO toca em conteúdo de clínica. Ele cria, configura e liga ou
desliga o tenant; paciente, conversa e prontuário não passam por aqui
(RF-ADM-6).
"""

from django.db import transaction
from django.utils import timezone
from rest_framework.exceptions import ValidationError

from apps.accounts.choices import MembershipRole
from apps.core.audit import log_action
from apps.core.models.audit_log import AuditAction
from apps.tenants.choices import ClinicStatus, SuspensionCategory
from apps.tenants.models import Clinic

# Teto do texto que explica a suspensão. O mesmo do Chatwoot, e a razão é a
# mesma: é para caber a explicação de um caso, não o histórico da negociação.
MAX_MOTIVO = 256


@transaction.atomic
def create_clinic(
    *,
    actor,
    name: str,
    slug: str,
    timezone_name: str,
    manager_name: str,
    manager_email: str,
    request=None,
) -> tuple[Clinic, str]:
    """
    Cria a clínica E o primeiro gestor (RF-ADM-1.2).

    Os dois juntos porque clínica sem gestor é clínica que ninguém acessa: ela
    nasceria só para o admin da plataforma olhar de fora, e alguém teria de
    lembrar de um segundo passo que nada cobra. O `manage.py clinica` já
    exigia o vínculo desde que nasceu.

    Devolve a clínica e a SENHA TEMPORÁRIA do gestor, que é dita uma vez e não
    volta a existir - a entrega é pessoal, porque não há e-mail transacional.
    """
    from apps.accounts.team import create_member

    slug = (slug or "").strip().lower()
    if Clinic.all_objects.filter(slug=slug).exists():
        # ⚠️ `all_objects`: o slug é único no banco INCLUSIVE para clínica
        # apagada por soft delete. Sem olhar as apagadas, o erro sairia como
        # 500 de violação de unicidade em vez de frase.
        raise ValidationError({"slug": "Já existe uma clínica com este endereço."})

    clinic = Clinic.objects.create(
        name=(name or "").strip(),
        slug=slug,
        timezone=timezone_name,
    )
    log_action(
        actor,
        AuditAction.CREATE,
        "Clinic",
        clinic.pk,
        payload={"operation": "clinic.create", "slug": slug, "origem": "plataforma"},
        request=request,
        clinic=clinic,
    )

    _, temporary_password = create_member(
        clinic=clinic,
        actor=actor,
        name=manager_name,
        email=manager_email,
        role=MembershipRole.MANAGER,
        request=request,
    )
    # ⚠️ Vem VAZIA quando o e-mail já tinha conta: a credencial dessa pessoa é
    # a que ela usa na outra clínica, e trocá-la aqui a derrubaria de lá. A
    # tela precisa dizer isso em vez de mostrar um campo em branco.
    return clinic, temporary_password


@transaction.atomic
def suspend_clinic(clinic: Clinic, *, actor, category: str, reason: str, request=None) -> Clinic:
    """
    Tira a clínica do ar (RF-ADM-1.4), e EXIGE dizer por quê.

    O motivo não é burocracia: sem ele, a pergunta "por que esta clínica está
    fora" fica sem resposta seis meses depois, quando quem suspendeu não
    lembra e o cliente está no telefone. É a mesma trava que o Chatwoot aplica
    no `SuperAdmin::AccountsController`.

    O que a suspensão faz está no `RF-ADM-1.7` e mora em três lugares:
    `resolve_active_membership` (a equipe), os sweeps de `automation/tasks.py`
    (os disparos) e `handle_inbound` (a resposta do robô). A ingestão continua.
    """
    reason = (reason or "").strip()
    if category not in SuspensionCategory.values:
        raise ValidationError({"suspension_category": "Escolha o motivo da suspensão."})
    if not reason:
        raise ValidationError({"suspension_reason": "Explique a suspensão em uma frase."})
    if len(reason) > MAX_MOTIVO:
        raise ValidationError(
            {"suspension_reason": f"O detalhe cabe em até {MAX_MOTIVO} caracteres."}
        )
    if clinic.is_suspended:
        raise ValidationError({"detail": "Esta clínica já está suspensa."})

    clinic.status = ClinicStatus.SUSPENDED
    clinic.suspension_category = category
    clinic.suspension_reason = reason
    clinic.suspended_at = timezone.now()
    clinic.save(
        update_fields=[
            "status",
            "suspension_category",
            "suspension_reason",
            "suspended_at",
            "updated_at",
        ]
    )

    # A auditoria É o histórico de suspensões (RF-ADM-1.5): a linha guarda a
    # categoria e o detalhe, então a sequência de idas e voltas se lê na tela
    # de auditoria sem tabela nova para isso.
    log_action(
        actor,
        AuditAction.UPDATE,
        "Clinic",
        clinic.pk,
        payload={
            "operation": "clinic.suspend",
            "category": category,
            "reason": reason,
        },
        request=request,
        clinic=clinic,
    )
    return clinic


@transaction.atomic
def reactivate_clinic(clinic: Clinic, *, actor, request=None) -> Clinic:
    """
    Devolve a clínica ao ar (RF-ADM-1.5).

    O motivo anterior é LIMPO do cadastro, mas não some: ele fica na linha da
    auditoria que suspendeu. O campo é o estado de agora, não o arquivo.
    """
    if not clinic.is_suspended:
        raise ValidationError({"detail": "Esta clínica já está ativa."})

    anterior = clinic.suspension_category
    clinic.status = ClinicStatus.ACTIVE
    clinic.suspension_category = ""
    clinic.suspension_reason = ""
    clinic.suspended_at = None
    clinic.save(
        update_fields=[
            "status",
            "suspension_category",
            "suspension_reason",
            "suspended_at",
            "updated_at",
        ]
    )

    log_action(
        actor,
        AuditAction.UPDATE,
        "Clinic",
        clinic.pk,
        payload={"operation": "clinic.reactivate", "category_anterior": anterior},
        request=request,
        clinic=clinic,
    )
    return clinic
