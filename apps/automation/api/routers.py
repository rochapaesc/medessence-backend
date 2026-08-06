from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.automation.api.viewsets import FlowRunViewSet, FlowViewSet

router = SimpleRouter()
router.register("flows", FlowViewSet, basename="flows")
router.register("flow-runs", FlowRunViewSet, basename="flow-runs")

urlpatterns = [
    path("", include(router.urls)),
]
