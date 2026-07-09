from django.contrib.admin import ModelAdmin


class AppointmentAdmin(ModelAdmin):
    list_display = (
        "id",
        "patient",
        "practitioner",
        "starts_at",
        "duration_min",
        "status",
        "source_status",
        "clinic",
        "sync_status",
    )
    list_filter = ("clinic", "status", "sync_status", "remotely", "practitioner")
    search_fields = ("patient__name", "practitioner__name", "external_id")
    autocomplete_fields = ("clinic", "patient", "practitioner", "care_unit", "procedure")
    readonly_fields = ("created_at", "updated_at", "deleted_at", "raw_payload")
    date_hierarchy = "starts_at"
