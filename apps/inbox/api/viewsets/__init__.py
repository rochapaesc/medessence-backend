from apps.inbox.api.viewsets.catalog import (
    QuickReplyViewSet,
    WhatsAppTemplateViewSet,
)
from apps.inbox.api.viewsets.contact_note import ContactNoteViewSet
from apps.inbox.api.viewsets.conversation import ConversationViewSet
from apps.inbox.api.viewsets.label import ConversationLabelViewSet
from apps.inbox.api.viewsets.media import MediaUploadViewSet
from apps.inbox.api.viewsets.message import MessageViewSet

__all__ = [
    "ContactNoteViewSet",
    "ConversationLabelViewSet",
    "ConversationViewSet",
    "MediaUploadViewSet",
    "MessageViewSet",
    "QuickReplyViewSet",
    "WhatsAppTemplateViewSet",
]
