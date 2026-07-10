from django.contrib.admin import ModelAdmin


class ChannelAdmin(ModelAdmin):
    list_display = ("id", "display_number", "provider", "phone_number_id", "clinic")
    list_filter = ("clinic", "provider")
    search_fields = ("display_number", "phone_number_id", "waba_id")
    autocomplete_fields = ("clinic",)
    readonly_fields = ("uuid", "created_at", "updated_at", "deleted_at")


class QuickReplyAdmin(ModelAdmin):
    list_display = ("id", "label", "clinic")
    list_filter = ("clinic",)
    search_fields = ("label", "body")
    autocomplete_fields = ("clinic",)
    readonly_fields = ("created_at", "updated_at", "deleted_at")


class WhatsAppTemplateAdmin(ModelAdmin):
    list_display = ("id", "name", "language", "category", "status", "clinic")
    list_filter = ("clinic", "status", "language", "category")
    search_fields = ("name",)
    autocomplete_fields = ("clinic",)
    readonly_fields = ("created_at", "updated_at", "deleted_at")
