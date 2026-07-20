from rest_framework import serializers

# ==================================================================
# 01.0 - [RESPONSE] Frontend Serializers
# ==================================================================

class StrListSerializer(serializers.ListSerializer):
    child = serializers.CharField()
