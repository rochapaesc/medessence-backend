import ast
import json

from cryptography.fernet import Fernet, InvalidToken
from django import forms
from django.conf import settings
from django.core.exceptions import ImproperlyConfigured
from django.core.serializers.json import DjangoJSONEncoder
from django.db import models


def _fernet() -> Fernet:
    key = getattr(settings, "FIELD_ENCRYPTION_KEY", None)
    if not key:
        raise ImproperlyConfigured(
            "FIELD_ENCRYPTION_KEY não configurada - necessária para EncryptedJSONField."
        )
    return Fernet(key.encode() if isinstance(key, str) else key)


class EncryptedJSONField(models.TextField):
    """
    JSON cifrado em repouso (Fernet/AES-128-CBC + HMAC).

    Usado para credenciais de integração por tenant (ex.: Clinic.ehr_credentials,
    Channel.credentials). O valor em Python é um dict/list normal; no banco fica
    um token Fernet opaco - sem filtro/índice sobre o conteúdo, por design.
    """

    description = "JSON cifrado em repouso (Fernet)"

    def __init__(self, *args, **kwargs):
        kwargs.setdefault("default", dict)
        super().__init__(*args, **kwargs)

    def get_prep_value(self, value):
        if value is None:
            return None
        raw = json.dumps(value, cls=DjangoJSONEncoder)
        return _fernet().encrypt(raw.encode()).decode()

    def from_db_value(self, value, expression, connection):
        if value is None or value == "":
            return {}
        try:
            return json.loads(_fernet().decrypt(value.encode()).decode())
        except InvalidToken as exc:
            raise ImproperlyConfigured(
                "Falha ao decifrar EncryptedJSONField - a FIELD_ENCRYPTION_KEY "
                "não corresponde à chave usada na escrita."
            ) from exc

    def to_python(self, value):
        # Deserialização (fixtures/forms): aceita dict/list já decifrado,
        # token Fernet, JSON puro ou literal Python (aspas simples no admin).
        if value is None or isinstance(value, (dict, list)):
            return value
        try:
            return json.loads(_fernet().decrypt(value.encode()).decode())
        except (InvalidToken, AttributeError):
            try:
                return json.loads(value)
            except (TypeError, ValueError):
                try:
                    return ast.literal_eval(value)
                except (SyntaxError, ValueError):
                    return value

    def formfield(self, **kwargs):
        # No admin, valida JSON na entrada (erro claro em vez de salvar string)
        kwargs.setdefault("form_class", forms.JSONField)
        kwargs.setdefault("help_text", 'JSON com aspas duplas - ex.: {"api_key": "..."}')
        return super().formfield(**kwargs)
