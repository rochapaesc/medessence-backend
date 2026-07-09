import os

from apps.core.utils.api_generator.generic_funcions import camel_to_snake, snake_to_camel


def generate_viewsets(models, app_name):
    base_path = f"apps/{app_name}/api/viewsets"
    if not os.path.exists(base_path):
        os.makedirs(base_path)
    for model_name in models:
        file_name = camel_to_snake(model_name)

        with open(f"{base_path}/{file_name}.py", "w+") as f:
            f.write(
                f"""
from apps.{app_name}.api.filtersets import {model_name}Filterset
from apps.{app_name}.api.serializers import (
    {model_name}CreateSerializer,
    {model_name}DetailSerializer,
    {model_name}ReadSerializer,
    {model_name}UpdateSerializer,
)
from apps.{app_name}.models import {model_name}
from apps.core.api.viewsets import BaseModelViewSet


class {model_name}ViewSet(BaseModelViewSet):
    model = {model_name}
    filterset_class = {model_name}Filterset
    serializer_class = {model_name}ReadSerializer

    action_serializer_classes = {{
        "list": {model_name}ReadSerializer,
        "retrieve": {model_name}DetailSerializer,
        "create": {model_name}CreateSerializer,
        "update": {model_name}UpdateSerializer,
        "partial_update": {model_name}UpdateSerializer,
    }}

"""
            )


def generate_viewset_init(app_name):
    all_files_on_viewsets = os.listdir(f"apps/{app_name}/api/viewsets")
    all_files_on_viewsets = [file for file in all_files_on_viewsets if file != "__init__.py"]
    str_imports = ""
    all_names = []
    for file in all_files_on_viewsets:
        file_name = file.replace(".py", "")
        viewset_name = snake_to_camel(file_name) + "ViewSet"
        all_names.append(viewset_name)

        str_imports += f"from apps.{app_name}.api.viewsets.{file_name} import {viewset_name}\n"

    export_all_names = [f'"{name}"' for name in all_names]

    str_imports += f"\n__all__ = [{', '.join(export_all_names)}]"

    with open(f"apps/{app_name}/api/viewsets/__init__.py", "w+") as f:
        f.write(str_imports)
