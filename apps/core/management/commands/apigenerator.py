from django.apps import apps
from django.core.management.base import BaseCommand, CommandError

from apps.core.utils import api_generator_from_app_model_names


class Command(BaseCommand):
    help = "List all models for a given app"

    def add_arguments(self, parser):
        parser.add_argument("app_label", type=str, help="The label of the application")

    def handle(self, *args, **options):
        app_label = options["app_label"]
        try:
            app_config = apps.get_app_config(app_label)
            models = app_config.get_models()
            model_names = [model.__name__ for model in models]
            api_generator_from_app_model_names(model_names, app_label)
            self.stdout.write(f"API's GERADAS PARA OS MODELS DA APP '{app_label}':")
            for model_name in model_names:
                self.stdout.write(f" - {model_name}")
        except LookupError as exp:
            raise CommandError(f"App '{app_label}' not found") from exp
        except Exception as e:
            raise CommandError(f"Error: {e}") from e
