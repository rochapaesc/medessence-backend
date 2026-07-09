import os

from apps.core.utils.api_generator.generic_funcions import camel_to_snake, snake_to_camel


def generate_filtersets(models, app_name):
    base_path = f"apps/{app_name}/api/filtersets"
    if not os.path.exists(base_path):
        os.makedirs(base_path)
    for model_name in models:
        file_name = camel_to_snake(model_name)
        with open(f"{base_path}/{file_name}.py", "w+") as f:
            f.write(
                f"""
from django_filters.rest_framework import FilterSet
from apps.{app_name}.models import {model_name}


class {model_name}Filterset(FilterSet):
    class Meta:
        model = {model_name}
        fields = []
"""
            )


def generate_filterset_init(app_name):
    all_files_on_filtersets = os.listdir(f"apps/{app_name}/api/filtersets")
    all_files_on_filtersets = [file for file in all_files_on_filtersets if file != "__init__.py"]
    str_imports = ""
    all_names = []
    for file in all_files_on_filtersets:
        file_name = file.replace(".py", "")
        filterset_name = snake_to_camel(file_name) + "Filterset"
        all_names.append(filterset_name)
        str_imports += f"from apps.{app_name}.api.filtersets.{file_name} import {filterset_name}\n"
    export_all_names = [f'"{name}"' for name in all_names]

    str_imports += f"\n__all__ = [{', '.join(export_all_names)}]"

    with open(f"apps/{app_name}/api/filtersets/__init__.py", "w+") as f:
        f.write(str_imports)
