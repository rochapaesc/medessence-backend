from django.contrib.admin import ModelAdmin, TabularInline

from apps.core.admin.audited import AuditedAdminMixin
from apps.tenants.models import ClinicBusinessHours


class ClinicBusinessHoursInline(TabularInline):
    """Dia sem linha = fechado o dia inteiro (RF-FLW-5.1.1)."""

    model = ClinicBusinessHours
    extra = 0
    fields = ("weekday", "opens_at", "closes_at")
    ordering = ("weekday",)


class ClinicAdmin(AuditedAdminMixin, ModelAdmin):
    list_display = (
        "id",
        "name",
        "slug",
        "timezone",
        "active_window_days",
        "ehr_provider",
        "created_at",
    )
    list_filter = ("ehr_provider",)
    search_fields = ("name", "slug")
    prepopulated_fields = {"slug": ("name",)}
    readonly_fields = ("created_at", "updated_at", "deleted_at")
    inlines = (ClinicBusinessHoursInline,)

    # A clínica É o tenant do evento.
    audit_clinic_field = "self"
