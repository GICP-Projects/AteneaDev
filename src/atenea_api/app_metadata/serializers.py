from rest_framework import serializers
from app_base.serializers import BulkListSerializer
from app_metadata.models import CategoryItem, EmbeddingsItem


class ListDataSerializer(BulkListSerializer):
    """ Custom Bulk List serializer to handle CategoryItems.
    """
    def get_unique_field_name(self):
        return "name"


# ==================================================================
# 01.0 - Category item Serializer
# ==================================================================

class CategoryItemSerializer(serializers.ModelSerializer):

    def to_internal_value(self, data):
        # Normalize name (capitalize and strip)
        if "name" in data:
            data["name"] = data["name"].capitalize().strip()
        return super().to_internal_value(data)

    class Meta:
        model = CategoryItem
        fields = [
            "id",
            "name",
            "description",
        ]
        list_serializer_class = ListDataSerializer


# ==================================================================
# 02.0 - Embeddings Search Serializers
# ==================================================================

class FilterDataSerializer(serializers.Serializer):
    name = serializers.CharField(
        max_length=128, 
        required=False,
        help_text="Filter by the name."
    )
    description = serializers.CharField(
        max_length=128, 
        required=False,
        help_text="Filter by the description."
    )


# ==================================================================
# 03.0 - Embeddings Search Serializers
# ==================================================================

class EmbeddingDocumentSerializer(serializers.Serializer):
    source_pk = serializers.CharField()
    text = serializers.CharField()
    lang = serializers.CharField(required=False, allow_blank=True, allow_null=True)
    score = serializers.FloatField(required=False)


# ==================================================================
# 04.0 - Embeddings Detail Serializers
# ==================================================================

class EmbeddingSerializer(serializers.ModelSerializer):
    class Meta:
        model = EmbeddingsItem
        fields = [
            'point_id',
            'collection_name',
            'vector_dim',
            'model',
            'version',
            'sync_status',
            'calculated_at',
            'synced_at',
        ]
