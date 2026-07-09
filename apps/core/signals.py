from django.contrib.auth.signals import (
    user_logged_in,
    user_logged_out,
    user_login_failed,
)
from django.dispatch import receiver

from apps.core.audit import log_action


@receiver(user_logged_in)
def on_login(sender, request, user, **kwargs):
    log_action(user, "LOGIN", "User", user.pk, request=request)


@receiver(user_logged_out)
def on_logout(sender, request, user, **kwargs):
    log_action(user, "LOGOUT", "User", user.pk, request=request)


@receiver(user_login_failed)
def on_login_failed(sender, credentials, request, **kwargs):
    log_action(
        user=None,
        action="LOGIN_FAILED",
        resource="User",
        resource_id=credentials.get("email", "?"),
        payload={"email": credentials.get("email")},
        request=request,
    )
