from django.contrib import admin
from .models import EntityItem, AnnotatedEntity

# Register your models here.
class EntityItemAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "type_name"]

    def get_readonly_fields(self, request, obj=None):
        return ("type",) if obj is not None else ()

admin.site.register(EntityItem, EntityItemAdmin)

class AnnotatedEntityAdmin(admin.ModelAdmin):
    list_display = ["id"]

admin.site.register(AnnotatedEntity, AnnotatedEntityAdmin)
