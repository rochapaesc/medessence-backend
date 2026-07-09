from django.contrib.admin import ModelAdmin


class MembershipAdmin(ModelAdmin):
    list_display = ("id", "user", "clinic", "role", "is_active", "created_at")
    list_filter = ("role", "is_active", "clinic")
    search_fields = ("user__email", "clinic__name")
    autocomplete_fields = ("user", "clinic")
