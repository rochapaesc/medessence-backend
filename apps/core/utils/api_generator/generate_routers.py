from apps.core.utils.api_generator.generic_funcions import camel_to_snake


def generate_routers_from_viewsets(models, app_name):
    viewset_names = [model + "ViewSet" for model in models]
    router_names = [camel_to_snake(model).replace("_", "-") + "s" for model in models]
    with open(f"apps/{app_name}/api/__init__.py", "w+") as f:
        pass

    router_register = ""
    for viewset_name, router_name in zip(viewset_names, router_names, strict=False):
        router_register += (
            f'router.register("{router_name}", {viewset_name}, basename="{router_name}")\n'
        )

    with open(f"apps/{app_name}/api/routers.py", "w+") as f:
        f.write(
            f"""
from django.urls import path, include
from rest_framework.routers import SimpleRouter
from apps.{app_name}.api.viewsets import {", ".join(viewset_names)}

router = SimpleRouter()
{router_register}

urlpatterns = [
    path("", include(router.urls)),
]
"""
        )
