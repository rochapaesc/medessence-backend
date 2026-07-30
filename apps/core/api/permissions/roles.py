from rest_framework.permissions import BasePermission

from apps.core.context import resolve_active_membership


class IsPlatformAdmin(BasePermission):
    """Plano plataforma - dono do SaaS. Não passa pelo contexto de clínica."""

    message = "Apenas administradores da plataforma podem acessar este recurso."

    def has_permission(self, request, view):
        user = request.user
        return bool(user and user.is_authenticated and user.is_platform_admin)


class IsClinicMember(BasePermission):
    """
    Qualquer papel do plano clínica. Resolver o contexto aqui garante que
    TODA request escopada valida o Membership antes de tocar o queryset -
    e que os erros do §3.1 (403/400) saem no lugar certo.
    """

    def has_permission(self, request, view):
        membership = resolve_active_membership(request)

        from apps.accounts.choices import MembershipRole

        if membership.role == MembershipRole.PARTNER:
            # A cerca do parceiro (RF-PAR-6), fail-closed: view nova nasce
            # FECHADA para ele a menos que declare `partner_allowed = True`.
            # É o que impede o papel externo de enxergar pacientes, agenda e
            # Inbox só por ter um Membership válido.
            self.message = "O papel Parceiro acessa apenas a área de Parceiros."
            return bool(getattr(view, "partner_allowed", False))
        return True


class RolePermission(BasePermission):
    """Base para permissões por papel de clínica."""

    allowed_roles: tuple = ()
    message = "Seu papel nesta clínica não permite esta ação."

    def has_permission(self, request, view):
        membership = resolve_active_membership(request)
        return membership.role in self.allowed_roles


def _roles(*values):
    return tuple(str(v) for v in values)


class IsClinicManager(RolePermission):
    """Gestor da clínica: billing, usuários, configurações, todas as carteiras."""

    message = "Apenas o gestor da clínica pode executar esta ação."

    @property
    def allowed_roles(self):
        from apps.accounts.choices import MembershipRole

        return _roles(MembershipRole.MANAGER)


class IsDoctor(RolePermission):
    """Médico (gestor incluso - quem pode o mais, pode o menos)."""

    @property
    def allowed_roles(self):
        from apps.accounts.choices import MembershipRole

        return _roles(MembershipRole.MANAGER, MembershipRole.DOCTOR)


class IsPartnerArea(RolePermission):
    """Área de Parceiros (RF-PAR-6): o gestor e o papel parceiro."""

    message = "Apenas o gestor e o parceiro acessam a área de Parceiros."

    @property
    def allowed_roles(self):
        from apps.accounts.choices import MembershipRole

        return _roles(MembershipRole.MANAGER, MembershipRole.PARTNER)
