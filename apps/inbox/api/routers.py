from django.urls import include, path
from rest_framework.routers import SimpleRouter

from apps.inbox.api.viewsets import (
    ConversationLabelViewSet,
    ConversationViewSet,
    MessageViewSet,
    QuickReplyViewSet,
    WhatsAppTemplateViewSet,
)

router = SimpleRouter()
router.register("conversations", ConversationViewSet, basename="conversations")
router.register("conversation-labels", ConversationLabelViewSet, basename="conversation-labels")
router.register("messages", MessageViewSet, basename="messages")
router.register("quick-replies", QuickReplyViewSet, basename="quick-replies")
router.register("wa-templates", WhatsAppTemplateViewSet, basename="wa-templates")

urlpatterns = [
    path("", include(router.urls)),
]
