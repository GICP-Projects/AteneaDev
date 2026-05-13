from datetime import datetime
from rest_framework import serializers
from rest_framework.settings import api_settings
from rest_framework.utils import html
from django.utils import timezone
from django.conf import settings
from django.utils.translation import gettext_lazy as _


# ==================================================================
# =====                Generic Response Serializer            ======
# ==================================================================
class GenericResponseSerializer(serializers.Serializer):
    token = serializers.UUIDField()
    message = serializers.CharField()
    results = serializers.JSONField(required=False)
    errors = serializers.JSONField(required=False)


# ==================================================================
# =====                 DateRange Serializer                  ======
# ==================================================================
class BaseDateRangeSerializer(serializers.Serializer):

    def validate(self, data):
        """Check that start is before end. Check start / end > min."""

        super().validate(data)
        start_field = self.get_start_field_name()
        end_field = self.get_end_field_name()

        if (start_field in data and end_field in data) and (data[start_field] > data[end_field]):
            raise serializers.ValidationError(
                _("End date must occur after start."),
            )
        if (start_field in data) and (data[start_field] < settings.MIN_DATE):
            raise serializers.ValidationError(
                _("Start date must occur after %(min_date)s."),
                params={"min_date": settings.MIN_DATE},
            )
        if (end_field in data) and (data[end_field] < settings.MIN_DATE):
            raise serializers.ValidationError(
                _("End date must occur after %(min_date)s."),
                params={"min_date": settings.MIN_DATE},
            )
        
        # datetime.date to datetime.datetime
        if data.get(start_field):
            dt_start = datetime.combine(data[start_field], datetime.min.time()) 
            data[start_field] = timezone.make_aware(dt_start)

        if data.get(end_field):
            dt_end = datetime.combine(data[end_field], datetime.max.time()) 
            data[end_field] = timezone.make_aware(dt_end)

        return data

    def get_start_field_name(self):
        raise NotImplementedError()

    def get_end_field_name(self):
        raise NotImplementedError()
    

class StandardDateRangeSerializer(BaseDateRangeSerializer):
    """Standard serializer.
    Use to validate an input date range and make it aware.
    TODO: ADD USER'S TIMEZONE IN THE INTERVAL F.E: '11/08/23 (UTC/GMT)'
    """

    date_start = serializers.DateField(
        format=settings.FORMAT_DATE, 
        input_formats=[settings.FORMAT_DATE], 
        required=False,
        help_text=(
            "Start date for the interval. date.now() >= date-start. The date should "
            f"be in the format: {settings.FORMAT_DATE}."
        )
    )
    date_end = serializers.DateField(
        format=settings.FORMAT_DATE, 
        input_formats=[settings.FORMAT_DATE], 
        required=False,
        help_text=(
            "Start date for the interval. date.now() <= date-end. The date should "
            f"be in the format: {settings.FORMAT_DATE}." 
        )
    )

    def get_start_field_name(self):
        return "date_start"

    def get_end_field_name(self):
        return "date_end"
    

# ==================================================================
# =====                  Bulk ListSerializer                  ======
# ==================================================================

class BulkListSerializer(serializers.ListSerializer):
    """ Custom serializer to handle bulk operations (create/update). This 
    serializers will try to bulk as much items as possible (ignoring items with 
    errors): "Best Effort philosophy".
    
    NOTE: get_unique_field_name() must be implemented in the child class.

    Instead of raising a serializers.ValidationError when an item fails to validate, it will
    mark the item as invalid and return a dictionary with the validated items and 
    the invalid items with the following structure:
    ```
    >>> serializer.validated_data
    {
        "items": [
            {
                "<field1>": str,
                "<field2>": str,
                "<field2>": List[str],
            },
            ...
        ],
        "invalid_items": {
            "<uniqe_field_value>": ["Reason 1 of the error",],
            ...
        }
    }
    ```

    This allows to process items without errors and return those that failed (with 
    the reasons of the errors).
    """

    def get_unique_field_name(self):
        raise NotImplementedError('`get_unique_field_name()` must be implemented.')

    def custom_data_validation(self, data):
        """ Custom data validation method, to customize how to handle each item
        and their validation errors.

        This method will check duplicated items and return a dictionary with:
            - Key `items` with the validated items
            - Key `invalid_items` containing the items with a serializers.ValidationError or 
            duplicates.

        Returns
        -------
        validated_data: dict
            A dictionary with the validated data. With `items` key containing a list of 
            validated items and `invalid_items` key containing a dictionary with the 
            invalid items. Structure:
            ```
            {
                "items": [
                    {
                        "<field1>": str,
                        "<field2>": str,
                        "<field2>": List[str],
                    },
                    ...
                ],
                "invalid_items": {
                    "<uniqe_field_value>": ["Reason 1 of the error",],
                    ...
                }
            }
            ```
        """

        unique_field_name = self.get_unique_field_name()

        # Validated unique field value as `key` and original unique field values as `value`
        duplicated_unq_values_by_validated_unq_value = {} 
        # Validated unique field value as `key` and validated data as `value`
        validated_data_by_unq_value = {} 
        valid_unique_values_set = set() # To avoid duplicated unique values
        error_items_by_unq_value = {}
        i=0
        for item in data:
            i+=1
            try:
                validated = self.run_child_validation(item.copy()) # To avoid modifying the original data
                valid_unique_value = validated[unique_field_name]
                # Log all unique_field values that points to the same resource (duplicated)
                duplicated_unq_values_by_validated_unq_value.setdefault(
                    valid_unique_value, []
                ).append(item[unique_field_name])

                # This resource has already appeared (duplicated), then remove from results
                if valid_unique_value in valid_unique_values_set:
                    validated_data_by_unq_value.pop(valid_unique_value, None) 
                else:
                    # Log aready existing items
                    valid_unique_values_set.add(valid_unique_value)
                    # Store the valid data 
                    validated_data_by_unq_value[valid_unique_value] = validated
            except serializers.ValidationError as exc:
                # In case link is not in the data (to identify the item)
                error_key = item.get(unique_field_name, f"Item nº{i}")
                error_items_by_unq_value[error_key] = {
                    field: errors 
                    for field, errors in exc.detail.items()
                }

        # Log the errors of the duplicated items
        for valid_unique_value, original_unq_values in duplicated_unq_values_by_validated_unq_value.items():
            if len(original_unq_values) > 1:
                for original_unq_value in original_unq_values:
                    error_items_by_unq_value[original_unq_value] = {
                        unique_field_name: [
                            f"Duplicated telegram resource '{valid_unique_value}' "
                            f"({len(original_unq_values)} items points to it, choose one)."
                        ]
                    }
        return {
            "items": list(validated_data_by_unq_value.values()), 
            "invalid_items": error_items_by_unq_value
        }

    def to_internal_value(self, data):
        """
        List of dicts of native values <- List of dicts of primitive datatypes.

        NOTE: Same method as ListSerializer.to_internal_value() but allowing to
        customize how to handle the data (by overriding `self.custom_data_validation()`)

        """
        if html.is_html_input(data):
            data = html.parse_html_list(data, default=[])

        if not isinstance(data, list):
            message = self.error_messages['not_a_list'].format(
                input_type=type(data).__name__
            )
            raise serializers.ValidationError({
                api_settings.NON_FIELD_ERRORS_KEY: [message]
            }, code='not_a_list')

        if not self.allow_empty and len(data) == 0:
            message = self.error_messages['empty']
            raise serializers.ValidationError({
                api_settings.NON_FIELD_ERRORS_KEY: [message]
            }, code='empty')

        if self.max_length is not None and len(data) > self.max_length:
            message = self.error_messages['max_length'].format(max_length=self.max_length)
            raise serializers.ValidationError({
                api_settings.NON_FIELD_ERRORS_KEY: [message]
            }, code='max_length')

        if self.min_length is not None and len(data) < self.min_length:
            message = self.error_messages['min_length'].format(min_length=self.min_length)
            raise serializers.ValidationError({
                api_settings.NON_FIELD_ERRORS_KEY: [message]
            }, code='min_length')

        return self.custom_data_validation(data)


# ==================================================================
# =====       Serializer to handle a list of PKs (uuid)        =====
# ==================================================================

class PKsSerializer(serializers.Serializer):
    """ A serializer to receive a list of primary keys from the client.
    """
    pks = serializers.ListField(
        child=serializers.UUIDField(format="hex"),
        min_length=1,
        max_length=1000 
    )