from rest_framework import serializers
from zoneinfo import available_timezones
from django.conf import settings
from django_celery_beat.models import (
    IntervalSchedule, 
    ClockedSchedule, 
    CrontabSchedule, 
    PeriodicTask
)
from app_telegram.serializers import (
    FilterRoomSerializer, 
    ScanRoomSerializer,
    SimpleFilterSeedSerializer,
    FilterMsgSerializer,
    EmbedFilterMsgSerializer
)
import json
from app_base.models import GeneralEncoder

# Any task except celery ones
#from celery import current_app
#TASK_CHOICES = list(sorted([task for task in current_app.tasks if task.startswith("app_")]))

# Dictionary with all the allowed tasks to be scheduled (using the API) and its 
# serializers (to validate their parameters)
ALLOWED_TASK_CHOICES = {
    "app_telegram.services.pipelines.scan_pipeline": ScanRoomSerializer,
    "app_telegram.services.pipelines.access_room_pipeline": FilterRoomSerializer,
    "app_telegram.services.pipelines.populate_pipeline": SimpleFilterSeedSerializer,
    "app_telegram.services.pipelines.index_msgs_pipeline": FilterMsgSerializer,
    "app_metadata.services.pipelines.embeddings_msgs_pipeline": EmbedFilterMsgSerializer,
    "app_entity.services.pipelines.ner_extraction_msgs_pipeline": FilterMsgSerializer
}



# ==================================================================
# 01.0 - Schedule items Serializers
# ==================================================================

class IntervalScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = IntervalSchedule
        fields = '__all__'

class ClockedScheduleSerializer(serializers.ModelSerializer):
    class Meta:
        model = ClockedSchedule
        fields = '__all__'
        #read_only_fields = ['id'] ID is read_only by default

class CrontabScheduleSerializer(serializers.ModelSerializer):
    # Map timezone fix to allow serialization (`timezone_field.TimeZoneField` is not JSON serializable)
    timezone = serializers.ChoiceField(choices=available_timezones())
    class Meta:
        model = CrontabSchedule
        fields = '__all__'


# ==================================================================
# 02.0 - PeriodicTask item Serializer
# ==================================================================
       
class PeriodicTaskSerializer(serializers.ModelSerializer):
    task = serializers.ChoiceField(
        choices=list(ALLOWED_TASK_CHOICES.keys()),
        required=True,
        help_text=(
            "Select a task to schedule."
        )
    )
    
    interval = IntervalScheduleSerializer(
        required=False,
        help_text=(
            "Interval Schedule to run the task on.  "
            "Set only one schedule type, leave the others null."
        ),
    )
    crontab = CrontabScheduleSerializer(
        required=False,
        help_text=(
            "Crontab Schedule to run the task on.  "
            "Set only one schedule type, leave the others null."
        ),
    )
    clocked = ClockedScheduleSerializer(
        required=False,
        help_text=(
            "Clocked Schedule to run the task on.  "
            "Set only one schedule type, leave the others null."
        ),
    )
    
    kwargs = serializers.JSONField(
        required=False, 
        help_text=(
            "Kwargs to be passed to the task. It will be validated with a different "
            "serializer depending on the task parameters."
        )
    )

    class Meta:
        model = PeriodicTask
        exclude = ['solar', 'args', 'queue', 'exchange', 'routing_key', 'headers'] 

    def validate(self, data):
        """ Make each schedule field mutually exlusive, only one type is allowed 
        """
        count = sum(
            1 for field in ["interval", "crontab", "clocked"] if field in data
        )
        if not count and not self.instance:
            # A scheduler must be set unless it's an update (instance is set)
            raise serializers.ValidationError(
                "One scheduler between 'clocked', 'interval' and 'crontab', must be set."
            )
        elif count > 1:
            raise serializers.ValidationError(
                "Only one scheduler ('interval', 'crontab', 'clocked') "
                "can be provided, not multiple."
            )
        
        # clocked must be one off task
        if "clocked" in data and not data.get("one_off", False):
            err_msg = '`clocked` must be one off, `one_off` must set True'
            raise serializers.ValidationError(err_msg)

        return super().validate(data)

    def validate_task(self, value):
        # If instance is set (it menas that the serializer is being used to update 
        # an existing item), the `task` can't change.
        if self.instance and self.instance.task != value:
            # This field will be read_only but it needs to be validated to avoid 
            # a wrong kwargs validation.
            raise serializers.ValidationError(
                "Cannot change the task of a scheduled task."
            )
        return value

    def validate_kwargs(self, value):
        """Validate the kwargs field depending on the task.

        It will return the kwargs in JSON encoded format (string).
        """
        # Dynamic field creation (to validate the kwargs depending on the task)
        task = self.initial_data.get("task")

        if not task:
            err_msg = '`task` is needed to validate the `kwargs` field'
            raise serializers.ValidationError(err_msg)
        if task in ALLOWED_TASK_CHOICES:
            serializer = ALLOWED_TASK_CHOICES.get(task)
            if serializer:
                validated_kwargs = serializer().run_validation(value)
                return json.dumps(validated_kwargs, cls=GeneralEncoder)

    def create(self, validated_data):
        """ Overwrite create method to allow nested writes (schedules).
        """
        # validate() will only allow one schedule type in the validated_data
        # The choosen schedule will be a dict, the rest will be none)
        for schedule_type in ["interval", "clocked", "crontab"]:
            if schedule_type in validated_data:
                ScheduleModel = self.fields[schedule_type].Meta.model
                # Don't create a new schedule if it already exists
                validated_data[schedule_type],_ = ScheduleModel.objects.get_or_create(**validated_data[schedule_type])
                break # Only one schedule type can be set
        return self.Meta.model.objects.create(**validated_data)

    def update(self, instance, validated_data):
        # validate() will only allow one schedule type in the validated_data
        # The choosen schedule will be a dict, the rest will be none)
        schedules_fields = set(["interval", "clocked", "crontab"])
        for schedule_type in schedules_fields:
            if schedule_type in validated_data:
                ScheduleModel = self.fields[schedule_type].Meta.model
                # Don't create a new schedule if it already exists
                validated_data[schedule_type],_ = ScheduleModel.objects.get_or_create(**validated_data[schedule_type])
                # Set to None the other schedules 
                for schedules_to_none in schedules_fields - set([schedule_type]):
                    setattr(instance, schedules_to_none, None)
                break # Only one schedule type can be set
        
        # Update the instance
        for key, value in validated_data.items():
            setattr(instance, key, value)

        instance.save()
        return instance


# ==================================================================
# 02.1 - [REQUEST] Filters to retrieve PeriodicTask items
# ==================================================================

class FilterPeriodicTaskSerializer(serializers.Serializer):
    task = serializers.ChoiceField(
        choices=list(ALLOWED_TASK_CHOICES.keys()),
        required=False,
        help_text=(
            "Scheduled task."
        )
    )
    name = serializers.CharField(
        max_length=200,
        required=False,
        help_text="Filter scheduled tasks by the name."
    )
    description = serializers.CharField(
        max_length=200,
        required=False,
        help_text="Filter scheduled tasks by the description."
    )
    schedule_type = serializers.ChoiceField(
        choices=['interval', 'crontab', 'clocked'],
        required=False,
        help_text=(
            "Filter scheduled tasks by their scheduling method "
            "('interval', 'crontab', 'clocked')."
        )
    )
