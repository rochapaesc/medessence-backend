from django.apps import AppConfig


class SchedulingConfig(AppConfig):
    default_auto_field = "django.db.models.BigAutoField"
    name = "apps.scheduling"
    verbose_name = "Agenda"

    def ready(self):
        import apps.scheduling.signals  # noqa: F401
