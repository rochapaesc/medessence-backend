from django.apps import AppConfig


class AutomationConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.automation"
    verbose_name = "Automação (fluxos e jornadas)"

    def ready(self):
        import apps.automation.signals  # noqa: F401
