from django.contrib import admin
from .models import EntityItem, AnnotatedEntity

# Register your models here.
class EntityItemAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "type"]

admin.site.register(EntityItem, EntityItemAdmin)

class AnnotatedEntityAdmin(admin.ModelAdmin):
    list_display = ["id"]

admin.site.register(AnnotatedEntity, AnnotatedEntityAdmin)