"""Troca da própria senha (RF-CTA-2 e RF-EQP-7)."""

from django.contrib.auth.password_validation import validate_password
from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework.serializers import CharField, Serializer, ValidationError


class PasswordChangeSerializer(Serializer):
    """
    A senha atual é obrigatória, e conferida ANTES de qualquer escrita.

    A exceção é quem está com senha temporária (RF-EQP-7): acabou de entrar
    com ela, então já provou que a tem, e cobrá-la de novo na tela de primeiro
    acesso só faria a pessoa procurar no papel o que ela digitou há um minuto.
    """

    current_password = CharField(write_only=True, required=False, allow_blank=True)
    new_password = CharField(write_only=True)

    @property
    def _user(self):
        return self.context["request"].user

    def validate_current_password(self, value):
        # Vazio aqui não é erro: quem cobra a presença é o `validate`, que
        # sabe se a senha em uso é temporária.
        if value and not self._user.check_password(value):
            raise ValidationError("A senha atual não confere.")
        return value

    def validate_new_password(self, value):
        try:
            validate_password(value, self._user)
        except DjangoValidationError as exc:
            raise ValidationError(list(exc.messages)) from exc
        return value

    def validate(self, attrs):
        user = self._user
        if not user.must_change_password and not attrs.get("current_password"):
            raise ValidationError({"current_password": ["Informe a sua senha atual."]})
        if user.check_password(attrs["new_password"]):
            raise ValidationError(
                {"new_password": ["A senha nova precisa ser diferente da atual."]}
            )
        return attrs
