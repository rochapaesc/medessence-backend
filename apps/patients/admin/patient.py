from django.contrib.admin import ModelAdmin, TabularInline

from apps.patients.choices import PatientStatus
from apps.patients.models import PatientContact, PatientTag


class PatientTagInline(TabularInline):
    model = PatientTag
    extra = 0
    fields = ("tag", "origin", "sync_status")
    autocomplete_fields = ("tag",)


class PatientContactInline(TabularInline):
    model = PatientContact
    extra = 0
    fields = ("contact", "is_primary")
    autocomplete_fields = ("contact",)


class PatientAdmin(ModelAdmin):
    list_display = (
        "id",
        "name",
        "cpf",
        "phone",
        "city",
        "clinic",
        "source",
        "last_appointment_at",
        "patient_status",
        "sync_status",
    )
    list_filter = ("clinic", "source", "sync_status", "gender", "state")
    list_select_related = ("clinic",)  # a coluna de status usa a janela da clínica
    search_fields = ("name", "cpf", "phone", "email", "external_id")
    readonly_fields = ("created_at", "updated_at", "deleted_at", "raw_payload")
    autocomplete_fields = ("clinic",)
    inlines = [PatientTagInline, PatientContactInline]
    date_hierarchy = "last_appointment_at"

    def patient_status(self, obj):
        return PatientStatus(obj.status).label

    patient_status.short_description = "Status"
