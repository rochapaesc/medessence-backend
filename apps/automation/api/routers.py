from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.automation.api.viewsets import (
    FlowRunViewSet,
    FlowViewSet,
    HttpDestinationViewSet,
    SequenceStepViewSet,
    SequenceViewSet,
)

router = SimpleRouter()
router.register("flows", FlowViewSet, basename="flows")
router.register("flow-runs", FlowRunViewSet, basename="flow-runs")
router.register("http-destinations", HttpDestinationViewSet, basename="http-destinations")
router.register("sequences", SequenceViewSet, basename="sequences")
router.register("sequence-steps", SequenceStepViewSet, basename="sequence-steps")

urlpatterns = [
    path("", include(router.urls)),
]
