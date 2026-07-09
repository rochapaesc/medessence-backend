from django.contrib.admin import ModelAdmin, site

from apps.integrations.models import SyncOperation, SyncRun


class SyncOperationAdmin(ModelAdmin):
    list_display = (
        "id",
        "clinic",
        "provider",
        "resource_type",
        "local_id",
        "status",
        "attempts",
        "created_at",
    )
    list_filter = ("clinic", "provider", "resource_type", "status")
    search_fields = ("local_id", "last_error")
    readonly_fields = ("created_at", "updated_at", "deleted_at", "payload")


class SyncRunAdmin(ModelAdmin):
    list_display = ("id", "clinic", "kind", "started_at", "finished_at", "created_at")
    list_filter = ("clinic", "kind")
    readonly_fields = ("created_at", "updated_at", "deleted_at", "stats")
    date_hierarchy = "started_at"


site.register(SyncOperation, SyncOperationAdmin)
site.register(SyncRun, SyncRunAdmin)
