import os

from apps.core.utils.api_generator.generic_funcions import camel_to_snake, snake_to_camel


def generate_serializer(models, app_name):
    base_path = f"apps/{app_name}/api/serializers"
    if not os.path.exists(base_path):
        os.makedirs(base_path)
    for model_name in models:
        file_name = camel_to_snake(model_name)

        with open(f"{base_path}/{file_name}.py", "w+") as f:
            f.write(
                f"""
from rest_framework.serializers import ModelSerializer
from apps.{app_name}.models import {model_name}


class {model_name}CreateSerializer(ModelSerializer):
    class Meta:
        model = {model_name}
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class {model_name}DetailSerializer(ModelSerializer):
    class Meta:
        model = {model_name}
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class {model_name}ReadSerializer(ModelSerializer):
    class Meta:
        model = {model_name}
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]


class {model_name}UpdateSerializer(ModelSerializer):
    class Meta:
        model = {model_name}
        fields = "__all__"
        read_only_fields = ["id", "created_at", "updated_at"]

"""
            )


def generate_serializer_init(app_name):
    all_files_on_serializers = os.listdir(f"apps/{app_name}/api/serializers")
    all_files_on_serializers = [file for file in all_files_on_serializers if file != "__init__.py"]
    str_imports = ""
    all_names = []
    for file in all_files_on_serializers:
        file_name = file.replace(".py", "")
        serializer_names = ", ".join(
            [
                snake_to_camel(file_name) + "CreateSerializer",
                snake_to_camel(file_name) + "DetailSerializer",
                snake_to_camel(file_name) + "ReadSerializer",
                snake_to_camel(file_name) + "UpdateSerializer",
            ]
        )
        all_names.append(serializer_names)
        str_imports += (
            f"from apps.{app_name}.api.serializers.{file_name} import {serializer_names}\n"
        )
    export_all_names = [f'"{name}"' for serializer in all_names for name in serializer.split(", ")]

    str_imports += f"\n__all__ = [{', '.join(export_all_names)}]"

    with open(f"apps/{app_name}/api/serializers/__init__.py", "w+") as f:
        f.write(str_imports)
