from django.contrib.admin import ModelAdmin
from django.utils.html import format_html


class TagAdmin(ModelAdmin):
    list_display = ("id", "name", "color_swatch", "clinic", "sync_scope", "identifier")
    list_filter = ("clinic", "sync_scope")
    search_fields = ("name", "external_id")
    autocomplete_fields = ("clinic",)

    def color_swatch(self, obj):
        return format_html(
            '<span style="display:inline-block;width:14px;height:14px;border-radius:4px;'
            'background:{};vertical-align:middle"></span> {}',
            obj.color,
            obj.color,
        )

    color_swatch.short_description = "Cor"
