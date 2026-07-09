from django.contrib.admin import site

from apps.inbox.admin.channel import (
    ChannelAdmin,
    QuickReplyAdmin,
    WhatsAppTemplateAdmin,
)
from apps.inbox.admin.conversation import (
    ConversationAdmin,
    MediaAssetAdmin,
    MessageAdmin,
    WebhookEventAdmin,
)
from apps.inbox.models import (
    Channel,
    Conversation,
    MediaAsset,
    Message,
    QuickReply,
    WebhookEvent,
    WhatsAppTemplate,
)

site.register(Channel, ChannelAdmin)
site.register(Conversation, ConversationAdmin)
site.register(Message, MessageAdmin)
site.register(MediaAsset, MediaAssetAdmin)
site.register(WebhookEvent, WebhookEventAdmin)
site.register(QuickReply, QuickReplyAdmin)
site.register(WhatsAppTemplate, WhatsAppTemplateAdmin)
