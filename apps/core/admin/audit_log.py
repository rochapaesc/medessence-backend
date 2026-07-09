from django.contrib.admin import ModelAdmin


class AuditLogAdmin(ModelAdmin):
    ordering = ("-timestamp",)

    list_display = (
        "id",
        "user",
        "action",
        "resource",
        "resource_id",
        "ip_address",
        "timestamp",
    )

    list_filter = (
        "action",
        "resource",
        "timestamp",
    )

    search_fields = (
        "user__email",
        "resource",
        "resource_id",
        "ip_address",
    )

    readonly_fields = (
        "user",
        "action",
        "resource",
        "resource_id",
        "payload",
        "ip_address",
        "timestamp",
    )

    list_select_related = ("user",)

    date_hierarchy = "timestamp"

    fieldsets = (
        (
            "Informações principais",
            {
                "fields": (
                    "user",
                    "action",
                    "timestamp",
                )
            },
        ),
        (
            "Recurso",
            {
                "fields": (
                    "resource",
                    "resource_id",
                )
            },
        ),
        (
            "Detalhes",
            {
                "fields": (
                    "payload",
                    "ip_address",
                )
            },
        ),
    )

    def has_add_permission(self, request):
        return False

    def has_change_permission(self, request, obj=None):
        return False

    def has_delete_permission(self, request, obj=None):
        return False
