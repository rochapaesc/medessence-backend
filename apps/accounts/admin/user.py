from django.contrib.admin import ModelAdmin, TabularInline

from apps.accounts.models import Membership
from apps.core.admin.audited import AuditedAdminMixin


class MembershipInline(TabularInline):
    model = Membership
    extra = 0
    fields = ("clinic", "role", "is_active")


class UserAdmin(AuditedAdminMixin, ModelAdmin):
    # Sem clínica de propósito: a conta é global, e o evento aparece para o
    # auditor da plataforma, não dentro de um tenant.
    list_display = ("id", "email", "first_name", "last_name", "is_platform_admin", "is_active")
    list_filter = ("is_platform_admin", "is_active", "is_staff")
    search_fields = ("email", "first_name", "last_name")
    readonly_fields = ("created_at", "last_login", "password")
    inlines = [MembershipInline]
