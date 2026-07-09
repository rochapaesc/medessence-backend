from django.contrib.admin import ModelAdmin


class ClinicAdmin(ModelAdmin):
    list_display = ("id", "name", "slug", "timezone", "ehr_provider", "created_at")
    list_filter = ("ehr_provider",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at", "deleted_at")
