from django.contrib.admin import ModelAdmin, TabularInline

from apps.accounts.models import Membership


class MembershipInline(TabularInline):
    model = Membership
    extra = 0
    fields = ("clinic", "role", "is_active")


class UserAdmin(ModelAdmin):
    list_display = ("id", "email", "first_name", "last_name", "is_platform_admin", "is_active")
    list_filter = ("is_platform_admin", "is_active", "is_staff")
    search_fields = ("email", "first_name", "last_name")
    readonly_fields = ("created_at", "last_login", "password")
    inlines = [MembershipInline]
