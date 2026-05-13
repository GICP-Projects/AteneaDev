from rest_framework import serializers
from app_telegram.serializers import SimpleMessageFilterSerializer

class StatsMsgSerializer(SimpleMessageFilterSerializer):
    """
    Serializer for message statistics endpoint.
    Validates the parameters for filtering and grouping messages.
    """
    GROUP_BY_CHOICES = (
        ('day', 'Day'),
        ('week', 'Week'),
        ('month', 'Month'),
        ('year', 'Year'),
    )
    group_by = serializers.ChoiceField(
        choices=GROUP_BY_CHOICES,
        default='day',
        help_text="Period to group messages by. 'day', 'week', 'month', 'year'."
    )

    z_score = serializers.BooleanField(
        default=False,
        help_text="If true, calculates the Z-Score for each group in addition to the message frequency."
    )

class StatsResultSerializer(serializers.Serializer):
    """
    Serializer for a single statistics result item.
    """
    period_start = serializers.DateTimeField()
    count = serializers.IntegerField()
    z_score = serializers.FloatField(required=False)  # Optional, only if z_score is calculated

