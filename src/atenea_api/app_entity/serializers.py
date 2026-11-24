from rest_framework import serializers

from app_entity.enums import EntityType


# ======================================================
# =====          ENTITY SERIALIZER FIELDS          =====
# ======================================================


class EntityTypeNameField(serializers.Field):
    """Expose numeric model codes as their stable textual names."""

    def to_representation(self, value):
        return EntityType.to_name(value)

    def to_internal_value(self, data):
        if not isinstance(data, str) or data.strip().isdigit():
            raise serializers.ValidationError("Expected a textual entity type name.")
        try:
            return EntityType.to_name(data)
        except ValueError as exc:
            raise serializers.ValidationError(str(exc)) from exc
