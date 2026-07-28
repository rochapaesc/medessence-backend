from django.contrib.admin import ModelAdmin, TabularInline

from apps.inbox.models import Message


class MessageInline(TabularInline):
    model = Message
    extra = 0
    fields = ("wa_timestamp", "direction", "sender_kind", "kind", "body", "status")
    readonly_fields = ("direction",)
    ordering = ("wa_timestamp",)
    show_change_link = True


class ConversationAdmin(ModelAdmin):
    list_display = (
        "id",
        "contact",
        "patient",
        "channel",
        "last_message_at",
        "unread_count",
        "status",
        "attended_by",
        "assigned_to",
        "clinic",
    )
    list_filter = ("clinic", "status", "attended_by", "channel")
    search_fields = ("contact__wa_id", "contact__display_name", "patient__name")
    autocomplete_fields = ("clinic", "channel", "contact", "patient", "assigned_to")
    readonly_fields = (
        "last_message_at",
        "last_inbound_at",
        "created_at",
        "updated_at",
        "deleted_at",
    )
    inlines = (MessageInline,)


class MessageAdmin(ModelAdmin):
    list_display = (
        "id",
        "conversation",
        "direction",
        "sender_kind",
        "kind",
        "status",
        "wa_timestamp",
        "clinic",
    )
    list_filter = ("clinic", "direction", "sender_kind", "kind", "status")
    search_fields = ("provider_message_id", "body")
    autocomplete_fields = ("clinic", "conversation", "media", "sent_by")
    readonly_fields = ("direction", "created_at", "updated_at", "deleted_at", "raw_payload")
    date_hierarchy = "wa_timestamp"


class MediaAssetAdmin(ModelAdmin):
    list_display = ("id", "provider_media_id", "mime_type", "size_bytes", "clinic")
    list_filter = ("clinic", "mime_type")
    search_fields = ("provider_media_id",)
    autocomplete_fields = ("clinic",)
    readonly_fields = ("created_at", "updated_at", "deleted_at")


class WebhookEventAdmin(ModelAdmin):
    list_display = ("id", "source", "clinic", "dedupe_key", "processed_at", "created_at")
    list_filter = ("source", "clinic", "processed_at")
    search_fields = ("dedupe_key",)
    readonly_fields = (
        "source",
        "clinic",
        "dedupe_key",
        "payload",
        "processed_at",
        "error",
        "created_at",
    )

    def has_add_permission(self, request):
        return False
