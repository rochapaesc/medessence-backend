from django.contrib.admin import ModelAdmin, TabularInline

from apps.scheduling.models import InsurancePlan


class PractitionerAdmin(ModelAdmin):
    list_display = (
        "id",
        "name",
        "license_number",
        "active_window_days",
        "clinic",
        "user",
        "external_id",
    )
    list_filter = ("clinic",)
    search_fields = ("name", "license_number", "external_id")
    autocomplete_fields = ("clinic", "user")


class CareUnitAdmin(ModelAdmin):
    list_display = ("id", "name", "clinic", "external_id")
    list_filter = ("clinic",)
    search_fields = ("name", "external_id")
    autocomplete_fields = ("clinic",)


class ProcedureAdmin(ModelAdmin):
    list_display = ("id", "name", "duration_min", "remotely", "clinic", "external_id")
    list_filter = ("clinic", "remotely")
    search_fields = ("name", "external_id")
    autocomplete_fields = ("clinic",)


class InsurancePlanInline(TabularInline):
    model = InsurancePlan
    extra = 0
    fields = ("name", "external_id")


class InsuranceCompanyAdmin(ModelAdmin):
    list_display = ("id", "name", "clinic", "external_id")
    list_filter = ("clinic",)
    search_fields = ("name", "external_id")
    autocomplete_fields = ("clinic",)
    inlines = [InsurancePlanInline]


class EHRStatusMapAdmin(ModelAdmin):
    list_display = ("id", "provider", "source_status", "status")
    list_filter = ("provider", "status")
    search_fields = ("source_status",)
