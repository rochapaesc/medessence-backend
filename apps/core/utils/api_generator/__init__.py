from apps.core.utils.api_generator.generate_filtersets import (
    generate_filterset_init,
    generate_filtersets,
)
from apps.core.utils.api_generator.generate_routers import generate_routers_from_viewsets
from apps.core.utils.api_generator.generate_serializers import (
    generate_serializer,
    generate_serializer_init,
)
from apps.core.utils.api_generator.generate_viewsets import generate_viewset_init, generate_viewsets


def api_generator_from_app_model_names(model_names, app_name):
    generate_serializer(model_names, app_name)
    generate_serializer_init(app_name)
    generate_filtersets(model_names, app_name)
    generate_filterset_init(app_name)
    generate_viewsets(model_names, app_name)
    generate_viewset_init(app_name)
    generate_routers_from_viewsets(model_names, app_name)
