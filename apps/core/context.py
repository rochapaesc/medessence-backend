"""
Contexto de clínica ativa (§3.1 do levantamento).

Cada request do plano clínica declara o contexto pelo header `X-Clinic-Id`.
O cliente declara o CONTEXTO, nunca a autorização: a autorização é o
Membership(user, clinic, is_active=True) validado aqui.

Regras:
    - Com header → valida o vínculo; sem vínculo ativo → 403.
    - Sem header + exatamente 1 vínculo ativo → auto-resolve.
    - Sem header + N vínculos → 400 `clinic_context_required` (o front abre
      o seletor de clínica e passa a enviar o header).

O membership resolvido é cacheado na request (`request.active_membership`).
"""

from rest_framework import status
from rest_framework.exceptions import (
    APIException,
    NotAuthenticated,
    PermissionDenied,
    ValidationError,
)

CLINIC_HEADER = "X-Clinic-Id"


class ClinicContextRequired(APIException):
    """400 — o front trata no interceptor e abre o seletor de clínica."""

    status_code = status.HTTP_400_BAD_REQUEST
    default_detail = (
        f"Você possui vínculo com mais de uma clínica. Informe o header {CLINIC_HEADER}."
    )
    default_code = "clinic_context_required"


def resolve_active_membership(request):
    """
    Resolve (e cacheia na request) o Membership ativo do usuário logado.

    Ponto único de resolução do contexto — viewsets escopados e permissions
    por papel passam por aqui.
    """
    cached = getattr(request, "active_membership", None)
    if cached is not None:
        return cached

    # Import tardio: core não pode importar accounts em tempo de carga de app.
    from apps.accounts.models import Membership

    user = request.user
    if not user or not user.is_authenticated:
        raise NotAuthenticated()

    memberships = Membership.objects.select_related("clinic").filter(
        user=user,
        is_active=True,
        clinic__deleted_at__isnull=True,
    )

    header = request.headers.get(CLINIC_HEADER)
    if header:
        try:
            clinic_id = int(header)
        except (TypeError, ValueError) as exc:
            raise ClinicContextRequired(f"{CLINIC_HEADER} inválido.") from exc
        membership = memberships.filter(clinic_id=clinic_id).first()
        if membership is None:
            raise PermissionDenied("Você não tem vínculo ativo com esta clínica.")
    else:
        found = list(memberships[:2])
        if not found:
            raise PermissionDenied("Você não tem vínculo ativo com nenhuma clínica.")
        if len(found) > 1:
            raise ClinicContextRequired()
        membership = found[0]

    request.active_membership = membership
    return membership


def assert_object_in_clinic(obj, clinic, label: str = "registro"):
    """
    Valida que uma FK aninhada pertence à clínica ativa (caminho de escrita).

    Nunca confiar em id vindo do cliente: todo objeto referenciado no payload
    passa por aqui antes do save.
    """
    if obj is None:
        return
    if getattr(obj, "clinic_id", None) != clinic.pk:
        raise ValidationError(f"O {label} informado não pertence à clínica ativa.")
