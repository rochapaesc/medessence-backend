import json
import logging

from django.core.serializers.json import DjangoJSONEncoder
from django.forms.models import model_to_dict

logger = logging.getLogger(__name__)

# Campos que NUNCA vão para o AuditLog
SENSITIVE_FIELDS = {
    "password",
    "new_password",
    "old_password",
    "token",
    "refresh",
    "access",
    "secret",
    "api_key",
}

# Marcador para campos sensíveis sanitizados
REDACTED = "***"


def get_client_ip(request) -> str | None:
    """Extrai o IP real do cliente, considerando proxies."""
    x_forwarded = request.META.get("HTTP_X_FORWARDED_FOR")
    if x_forwarded:
        return x_forwarded.split(",")[0].strip()
    return request.META.get("REMOTE_ADDR")


def _to_json_safe(value):
    """
    Converte um valor para tipos JSON primitivos puros (str/dict/list/int/float/bool/None).

    Usa DjangoJSONEncoder para lidar com date, datetime, Decimal, UUID, etc.,
    e faz round-trip via json.loads para garantir que o valor final não dependa
    de encoder customizado — o driver do Postgres usa o json.dumps padrão.
    """
    if value is None or isinstance(value, (bool, int, float, str)):
        return value
    try:
        return json.loads(json.dumps(value, cls=DjangoJSONEncoder))
    except (TypeError, ValueError):
        return str(value)


def sanitize_payload(data) -> dict:
    """
    Garante um dict JSON-safe e sem campos sensíveis.
    Aceita dict, OrderedDict, ou qualquer mapping; demais tipos viram {}.
    """
    if not data:
        return {}
    if not hasattr(data, "items"):
        return {}

    cleaned = {}
    for key, value in data.items():
        if key in SENSITIVE_FIELDS:
            cleaned[key] = REDACTED
        else:
            cleaned[key] = _to_json_safe(value)
    return cleaned


def snapshot_instance(instance, fields: list[str] | None = None) -> dict:
    """
    Snapshot serializável do estado de uma instância.
    FKs viram FK_id (model_to_dict já cuida disso).
    Demais tipos passam pelo sanitize_payload.
    """
    try:
        data = model_to_dict(instance, fields=fields)
    except Exception:
        logger.exception("Falha ao gerar snapshot de %s", instance)
        return {}
    return sanitize_payload(data)


def log_action(
    user,
    action: str,
    resource: str,
    resource_id,
    payload: dict | None = None,
    request=None,
    clinic=None,
) -> None:
    """
    Registra uma ação no log de auditoria.
    NUNCA propaga exceção — falha de auditoria não deve quebrar a request.

    `clinic` escopa o log no tenant (nula em eventos globais, ex.: login).
    """
    from apps.core.models import AuditLog

    try:
        AuditLog.objects.create(
            user=user if (user and getattr(user, "is_authenticated", False)) else None,
            clinic=clinic,
            action=action,
            resource=resource,
            resource_id=str(resource_id),
            payload=sanitize_payload(payload),
            ip_address=get_client_ip(request) if request else None,
        )
    except Exception:
        logger.exception(
            "Falha ao registrar AuditLog (action=%s, resource=%s, id=%s)",
            action,
            resource,
            resource_id,
        )
