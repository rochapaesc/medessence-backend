from django.contrib.admin import ModelAdmin

from apps.core.admin.audited import AuditedAdminMixin
from apps.scheduling.models import Practitioner


class MembershipAdmin(AuditedAdminMixin, ModelAdmin):
    # Quem entra e quem sai da clínica: o evento mais sensível do admin.
    audit_clinic_field = "clinic"

    list_display = ("id", "user", "clinic", "role", "practitioner", "is_active", "created_at")
    list_filter = ("role", "is_active", "clinic")
    list_select_related = ("user", "clinic", "practitioner")
    search_fields = ("user__email", "clinic__name")
    autocomplete_fields = ("user", "clinic")

    def formfield_for_foreignkey(self, db_field, request, **kwargs):
        """
        O dropdown de profissional mostrava só o nome - sem a clínica é
        impossível ver que o profissional é de OUTRO tenant, e foi assim que um
        vínculo cross-clinic passou despercebido (21/07/2026). A recusa em si é
        do `Membership.clean()`; aqui só deixamos a escolha legível.
        """
        if db_field.name == "practitioner":
            kwargs["queryset"] = Practitioner.objects.select_related("clinic").order_by(
                "clinic__name", "name"
            )
            formfield = super().formfield_for_foreignkey(db_field, request, **kwargs)
            formfield.label_from_instance = lambda obj: f"{obj.name} — {obj.clinic.name}"
            return formfield
        return super().formfield_for_foreignkey(db_field, request, **kwargs)
