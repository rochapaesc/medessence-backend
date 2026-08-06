from django.contrib.admin import ModelAdmin, TabularInline

from apps.automation.models import FlowRunEvent, FlowVersion


class FlowVersionInline(TabularInline):
    model = FlowVersion
    extra = 0
    fields = ("number", "published_at", "created_at")
    readonly_fields = ("created_at",)
    ordering = ("-number",)


class FlowAdmin(ModelAdmin):
    list_display = ("name", "clinic", "status", "trigger", "only_outside_hours", "priority")
    list_filter = ("clinic", "status", "trigger", "only_outside_hours")
    search_fields = ("name",)
    inlines = (FlowVersionInline,)


class FlowVersionAdmin(ModelAdmin):
    list_display = ("flow", "number", "published_at")
    list_filter = ("flow__clinic",)


class FlowRunEventInline(TabularInline):
    model = FlowRunEvent
    extra = 0
    fields = ("created_at", "event_type", "node_key", "data")
    readonly_fields = fields
    ordering = ("created_at",)

    def has_add_permission(self, request, obj=None):
        return False


class FlowRunAdmin(ModelAdmin):
    list_display = ("id", "flow", "contact", "status", "current_node", "last_advanced_at")
    list_filter = ("clinic", "status", "flow")
    date_hierarchy = "created_at"
    inlines = (FlowRunEventInline,)
