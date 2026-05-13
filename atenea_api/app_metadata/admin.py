from django import forms
from django.contrib import admin
from .models import (
    EmbeddingsItem, 
    CategoryItem, 
    SimilarityCategory
)


# ======================================================
# =====         EMBED ADMIN FUNCTIONALITY          =====
# ======================================================
class EmbeddingsItemAdmin(admin.ModelAdmin):
    list_display = ["id", "model", "version", "collection_name", "sync_status", "calculated_at"]
    readonly_fields = [
        "collection_name",
        "point_id",
        "vector_dim",
        "text_hash",
        "sync_status",
        "sync_error",
        "synced_at",
        "calculated_at",
    ]

admin.site.register(EmbeddingsItem, EmbeddingsItemAdmin)


# ======================================================
# =====       CATEGORY ADMIN FUNCTIONALITY         =====
# ======================================================
class CategoryItemAdmin(admin.ModelAdmin):
    list_display = ["id", "name", "get_embeddings", "last_update"]
    readonly_fields= ["last_update"]

    @admin.display(description="Embeddings model", ordering="embeddings__model")
    def get_embeddings(self, obj):
        if obj.embeddings:
            return f"{obj.embeddings.model}_{obj.embeddings.version}"
        return "(Empty)"

admin.site.register(CategoryItem, CategoryItemAdmin)

class SimilarityCategoryAdmin(admin.ModelAdmin):
    list_display = ["id", "get_category_name", "object_id", "calculated_at"]
    readonly_fields= ["similarity"]

    @admin.display(description="Categories", ordering="category__name")
    def get_category_name(self, obj):
        if obj.category:
            return f"{obj.category.name}"
        return "(Empty)"
    
admin.site.register(SimilarityCategory, SimilarityCategoryAdmin)
