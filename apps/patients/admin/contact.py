from django.contrib.admin import ModelAdmin, TabularInline

from apps.patients.models import PatientContact


class ContactPatientsInline(TabularInline):
    model = PatientContact
    extra = 0
    fields = ("patient", "is_primary")
    autocomplete_fields = ("patient",)


class ContactAdmin(ModelAdmin):
    list_display = ("id", "wa_id", "display_name", "clinic", "created_at")
    list_filter = ("clinic",)
    search_fields = ("wa_id", "display_name")
    autocomplete_fields = ("clinic",)
    inlines = [ContactPatientsInline]
