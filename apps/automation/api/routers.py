from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.automation.api.viewsets import (
    FlowRunViewSet,
    FlowViewSet,
    SequenceStepViewSet,
    SequenceViewSet,
)

router = SimpleRouter()
router.register("flows", FlowViewSet, basename="flows")
router.register("flow-runs", FlowRunViewSet, basename="flow-runs")
router.register("sequences", SequenceViewSet, basename="sequences")
router.register("sequence-steps", SequenceStepViewSet, basename="sequence-steps")

urlpatterns = [
    path("", include(router.urls)),
]
