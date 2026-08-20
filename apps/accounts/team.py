"""
As regras da equipe da clínica (§4.12, RF-EQP-1..9).

Fora do viewset porque a autorização de verdade é aqui: a camada HTTP só
traduz erro. É o desenho das RPCs do wacrm, que revalidam tudo do zero mesmo
já tendo passado por um `requireRole('admin')` na rota - quem confia na camada
de cima acaba com uma porta sem cerca no dia em que alguém acrescenta outra
rota para a mesma operação.
"""

from django.db import transaction
from rest_framework.exceptions import PermissionDenied

from apps.accounts.choices import MembershipRole
from apps.accounts.models import Membership, User
from apps.accounts.passwords import generate_temporary_password, set_user_password
from apps.core.audit import log_action
from apps.core.models.audit_log import AuditAction


def _refuse(detail: str, code: str):
    """Bloqueio com a frase que a tela mostra e o código que ela reconhece."""
    raise PermissionDenied({"detail": detail, "code": code})


def split_name(name: str) -> tuple[str, str]:
    first, _, last = (name or "").strip().partition(" ")
    return first, last


def assert_not_self(membership: Membership, actor: User, detail: str) -> None:
    """
    O gestor não age sobre a própria conta por esta tela (RF-EQP-8).

    É a proteção mais barata contra alguém se trancar para fora, e as três
    referências convergem nela.
    """
    if membership.user_id == actor.pk:
        _refuse(detail, "self_target")


def assert_not_last_manager(membership: Membership, *, action: str) -> None:
    """
    A clínica não pode ficar sem gestor ativo (RF-EQP-8).

    ⚠️ Vale para DESATIVAR e para REBAIXAR. O chatwoot só esconde o botão no
    front (`Index.vue`) e a API aceita a remoção; nenhuma das três referências
    protege o rebaixamento, que deixa a clínica sem quem administre e sem
    volta - a partir daí só o fornecedor conserta.

    ⚠️ Hoje ela é rede de segurança, não o guarda da porta: como só o gestor
    alcança esta tela, quem age já é um segundo gestor ativo e o alvo nunca é
    o último (quem tenta sair sozinho é barrado antes por `assert_not_self`).
    Ela existe para o dia em que outra porta chegar aqui - o plano da
    plataforma (§4.8), um comando, ou uma mudança na regra de papel.
    """
    if membership.role != MembershipRole.MANAGER or not membership.is_active:
        return

    outros = (
        Membership.objects.filter(
            clinic_id=membership.clinic_id,
            role=MembershipRole.MANAGER,
            is_active=True,
            deleted_at__isnull=True,
        )
        .exclude(pk=membership.pk)
        .exists()
    )
    if not outros:
        verbo = "desativar" if action == "deactivate" else "trocar o papel de"
        _refuse(
            f"Esta clínica ficaria sem nenhum gestor. Promova outra pessoa a "
            f"gestor antes de {verbo} esta.",
            "last_manager",
        )


@transaction.atomic
def create_member(
    *,
    clinic,
    actor: User,
    email: str,
    name: str,
    role: str,
    practitioner=None,
    request=None,
) -> tuple[Membership, str | None]:
    """
    Cria (ou revive) o vínculo, e devolve a senha temporária quando houve uma.

    ⚠️ O e-mail decide o caminho (é o `AgentBuilder#find_or_create_user` do
    chatwoot): quem JÁ tem conta não recebe senha nova nem nome novo, porque
    essa é a credencial que a pessoa usa na OUTRA clínica, e trocá-la aqui a
    derrubaria de lá.
    """
    email = (email or "").strip().lower()
    user = User.objects.filter(email__iexact=email).first()
    temporary_password = None

    if user is None:
        temporary_password = generate_temporary_password()
        first_name, last_name = split_name(name)
        user = User.objects.create_user(
            email=email,
            password=temporary_password,
            first_name=first_name,
            last_name=last_name,
        )
        set_user_password(user, temporary_password, temporary=True)
        log_action(
            actor,
            AuditAction.CREATE,
            "User",
            user.pk,
            payload={"email": email},
            request=request,
            clinic=clinic,
        )

    membership = Membership.objects.filter(user=user, clinic=clinic).first()
    if membership is not None and membership.is_active:
        _refuse("Esta pessoa já faz parte da equipe desta clínica.", "already_member")

    if membership is None:
        membership = Membership(user=user, clinic=clinic)
    membership.role = role
    membership.practitioner = practitioner
    membership.is_active = True
    membership.deleted_at = None
    # `full_clean` é quem cobra as regras de carteira do M3 (profissional de
    # outra clínica, carteira já usada, papel que não é médico).
    membership.full_clean(exclude=["deleted_at"])
    membership.save()

    log_action(
        actor,
        AuditAction.CREATE,
        "Membership",
        membership.pk,
        payload={"user": user.email, "role": role},
        request=request,
        clinic=clinic,
    )
    return membership, temporary_password


@transaction.atomic
def update_member(
    *,
    membership: Membership,
    actor: User,
    name: str | None = None,
    role: str | None = None,
    practitioner=...,
    request=None,
) -> Membership:
    """Papel, carteira e nome. O e-mail nunca (RF-EQP-4)."""
    changed = []

    if role is not None and role != membership.role:
        assert_not_self(membership, actor, "Você não pode mudar o seu próprio papel.")
        assert_not_last_manager(membership, action="demote")
        membership.role = role
        changed.append("role")

    if practitioner is not ... and practitioner != membership.practitioner:
        membership.practitioner = practitioner
        changed.append("practitioner")

    if changed:
        membership.full_clean(exclude=["deleted_at"])
        membership.save(update_fields=["role", "practitioner", "updated_at"])

    if name:
        first_name, last_name = split_name(name)
        user = membership.user
        if (first_name, last_name) != (user.first_name, user.last_name):
            user.first_name, user.last_name = first_name, last_name
            user.save(update_fields=["first_name", "last_name"])
            changed.append("name")

    if changed:
        log_action(
            actor,
            AuditAction.UPDATE,
            "Membership",
            membership.pk,
            payload={"changed_fields": changed},
            request=request,
            clinic=membership.clinic,
        )
    return membership


@transaction.atomic
def deactivate_member(*, membership: Membership, actor: User, request=None) -> int:
    """
    Revoga o acesso e devolve quantas conversas voltaram para a fila.

    Nunca apaga o `User`: ele pode ter vínculo com outra clínica e carrega o
    histórico de quem atendeu o quê.
    """
    from apps.inbox.attendance import release_conversations_of

    assert_not_self(membership, actor, "Você não pode desativar a sua própria conta.")
    assert_not_last_manager(membership, action="deactivate")

    membership.is_active = False
    membership.save(update_fields=["is_active", "updated_at"])
    log_action(
        actor,
        AuditAction.UPDATE,
        "Membership",
        membership.pk,
        payload={"changed_fields": ["is_active"], "is_active": False},
        request=request,
        clinic=membership.clinic,
    )
    return release_conversations_of(membership.user, membership.clinic)


@transaction.atomic
def reactivate_member(*, membership: Membership, actor: User, request=None) -> Membership:
    membership.is_active = True
    membership.save(update_fields=["is_active", "updated_at"])
    log_action(
        actor,
        AuditAction.UPDATE,
        "Membership",
        membership.pk,
        payload={"changed_fields": ["is_active"], "is_active": True},
        request=request,
        clinic=membership.clinic,
    )
    return membership


@transaction.atomic
def reset_member_password(*, membership: Membership, actor: User, request=None) -> str:
    """
    Gera senha temporária nova para um membro (RF-EQP-6).

    O gestor não reseta a própria senha por aqui: para isso existe o Meu
    perfil, que cobra a senha atual. Resetar a si mesmo seria trocar a própria
    senha sem provar que sabe a atual, com o computador aberto no balcão.
    """
    assert_not_self(
        membership,
        actor,
        "Para trocar a sua própria senha, vá em Meu perfil.",
    )

    temporary_password = generate_temporary_password()
    set_user_password(membership.user, temporary_password, temporary=True)
    log_action(
        actor,
        AuditAction.PASSWORD_RESET,
        "User",
        membership.user_id,
        # Quem redefiniu a senha de quem. O valor NUNCA entra.
        payload={"email": membership.user.email},
        request=request,
        clinic=membership.clinic,
    )
    return temporary_password
