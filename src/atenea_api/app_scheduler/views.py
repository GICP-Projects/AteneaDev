import logging

from django.core.exceptions import ValidationError as DjangoValidationError
from rest_framework import status, viewsets
from rest_framework.decorators import action
from rest_framework.exceptions import ValidationError as DRFValidationError
from rest_framework.permissions import IsAdminUser
from rest_framework.response import Response
from drf_spectacular.utils import extend_schema, OpenApiResponse
from django_celery_beat.models import PeriodicTask

from app_base.serializers import (
    GenericResponseSerializer,
    GenericResultResponseSerializer,
)
from app_base.views import (
    BasePaginator,
    create_message,
    create_paginate_response,
    create_result_response,
    get_and_log_request_data,
    manage_crud_request,
)
from app_scheduler.serializers import (
    ProcessingBatchSerializer,
    ProcessingJobListSerializer,
    ProcessingJobSerializer,
    ProcessingTriggerFilterSerializer,
    ProcessingTriggerSerializer,
)
from app_scheduler.services.jobs import (
    cancel_job,
    discard_failed_job,
    InvalidJobAction,
    pause_job,
    processing_capacity,
    resume_job,
    retry_failed_job,
    retry_failed_job_tree,
)
from app_scheduler.models import ProcessingJob
from app_scheduler.services import api as scheduler_api

logger = logging.getLogger(__name__)


class ProcessingTriggerViewSet(viewsets.ViewSet):
    """Admin API for periodic rules that launch registered ProcessingJob pipelines."""


    # https://www.django-rest-framework.org/api-guide/throttling/#scopedratethrottle
    throttle_scope = "staff_api" # thorttle_scope to restrict access and control the number of requests
    permission_classes = [IsAdminUser]
    pagination_class = BasePaginator

    @extend_schema(
        request=ProcessingTriggerSerializer,
        responses={
            status.HTTP_201_CREATED: GenericResultResponseSerializer,
            status.HTTP_400_BAD_REQUEST: GenericResponseSerializer,
        },
        tags=["Platform configuration"],
        summary="Create a periodic ProcessingJob pipeline trigger.",
        description=(
            "The pipeline key selects an application-owned Job launcher. "
            "Celery task names and internal Beat tasks cannot be supplied."
        ),
    )
    def create(self, request):
        return manage_crud_request(
            request=request,
            crud_action="create",
            ModelSerializerClass=ProcessingTriggerSerializer,
            success_status_code=status.HTTP_201_CREATED,
        )

    @extend_schema(
        parameters=[ProcessingTriggerFilterSerializer],
        responses={status.HTTP_200_OK: ProcessingTriggerSerializer(many=True)},
        tags=["Platform configuration"],
        summary="List scheduled ProcessingJob pipeline triggers.",
    )
    def list(self, request):
        filters = ProcessingTriggerFilterSerializer(data=request.query_params)
        filters.is_valid(raise_exception=True)
        queryset = scheduler_api.search_triggers(**filters.validated_data)
        return create_paginate_response(
            token=None,
            queryset=queryset,
            request=request,
            serializer_class=ProcessingTriggerSerializer,
        )

    @extend_schema(
        responses={status.HTTP_200_OK: GenericResultResponseSerializer},
        tags=["Platform configuration"],
        summary="Retrieve a scheduled pipeline trigger.",
    )
    def retrieve(self, request, pk=None):
        try:
            trigger = scheduler_api.get_trigger(pk)
        except PeriodicTask.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return create_result_response(
            token=None,
            result=ProcessingTriggerSerializer(
                instance=trigger,
                context={"request": request},
            ).data,
        )

    @extend_schema(
        request=ProcessingTriggerSerializer(partial=True),
        responses={
            status.HTTP_200_OK: GenericResultResponseSerializer,
            status.HTTP_400_BAD_REQUEST: GenericResponseSerializer,
            status.HTTP_404_NOT_FOUND: GenericResponseSerializer,
        },
        tags=["Platform configuration"],
        summary="Update a scheduled pipeline trigger.",
    )
    def partial_update(self, request, pk=None):
        return manage_crud_request(
            request=request,
            crud_action="update",
            ModelSerializerClass=ProcessingTriggerSerializer,
            item_pk=pk,
            extra_args={"partial": True},
            model_queryset=scheduler_api.public_trigger_queryset(),
        )

    @extend_schema(
        responses={
            status.HTTP_204_NO_CONTENT: OpenApiResponse(description="Trigger deleted."),
            status.HTTP_404_NOT_FOUND: GenericResponseSerializer,
        },
        tags=["Platform configuration"],
        summary="Delete a scheduled pipeline trigger.",
    )
    def destroy(self, request, pk=None):
        return manage_crud_request(
            request=request,
            crud_action="destroy",
            ModelSerializerClass=ProcessingTriggerSerializer,
            item_pk=pk,
            model_queryset=scheduler_api.public_trigger_queryset(),
        )


class ProcessingJobViewSet(viewsets.GenericViewSet):
    """Admin-only operational API for durable processing jobs."""

    throttle_scope = "staff_api"
    permission_classes = [IsAdminUser]
    serializer_class = ProcessingJobSerializer
    pagination_class = BasePaginator

    def _get_job(self, pk):
        return ProcessingJob.objects.get(pk=pk)

    @extend_schema(
        parameters=[ProcessingJobListSerializer],
        responses={status.HTTP_200_OK: ProcessingJobSerializer(many=True)},
        tags=["Data processing endpoints"],
    )
    def list(self, request):
        serializer = ProcessingJobListSerializer(data=request.query_params)
        serializer.is_valid(raise_exception=True)
        queryset = ProcessingJob.objects.all().order_by("-created_at")
        for key, value in serializer.validated_data.items():
            queryset = queryset.filter(**{key: value})
        page = self.paginate_queryset(queryset)
        result_serializer = self.get_serializer(page, many=True)
        return self.get_paginated_response(result_serializer.data)

    @extend_schema(
        responses={status.HTTP_200_OK: GenericResultResponseSerializer},
        tags=["Data processing endpoints"],
    )
    def retrieve(self, request, pk=None):
        try:
            job = self._get_job(pk)
        except ProcessingJob.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        return create_result_response(
            token=None,
            result=ProcessingJobSerializer(
                instance=job,
                context={"request": request},
            ).data,
        )

    @extend_schema(
        responses={status.HTTP_200_OK: ProcessingBatchSerializer(many=True)},
        tags=["Data processing endpoints"],
    )
    @action(methods=["get"], detail=True, url_path="batches")
    def batches(self, request, pk=None):
        try:
            job = self._get_job(pk)
        except ProcessingJob.DoesNotExist:
            return Response(status=status.HTTP_404_NOT_FOUND)
        queryset = job.batches.order_by("ordinal")
        page = self.paginate_queryset(queryset)
        result_serializer = ProcessingBatchSerializer(
            page,
            many=True,
            context=self.get_serializer_context(),
        )
        return self.get_paginated_response(result_serializer.data)

    def _change_state(self, request, pk, operation):
        _, token = get_and_log_request_data(request=request)
        try:
            job = self._get_job(pk)
            job = operation(job.pk)
        except ProcessingJob.DoesNotExist:
            return create_message(
                token,
                status.HTTP_404_NOT_FOUND,
                "There is no element with that id.",
            )
        except InvalidJobAction as e:
            logger.info(f"{e.__class__.__name__}: {e}")
            return create_message(
                token,
                status.HTTP_400_BAD_REQUEST,
                str(e),
            )
        except (TypeError, ValueError, DjangoValidationError) as e:
            logger.info(f"{e.__class__.__name__}: {e}")
            return create_message(
                token,
                status.HTTP_404_NOT_FOUND,
                "There is no element with that id.",
            )
        except DRFValidationError as e:
            logger.info(f"{e.__class__.__name__}: {e.detail}")
            return create_message(
                token,
                status.HTTP_400_BAD_REQUEST,
                "There were some issues with the input data.",
                add_errors=e.detail,
            )
        except Exception as e:
            logger.exception(f"{e.__class__.__name__}: {e}")
            return create_message(token, status.HTTP_500_INTERNAL_SERVER_ERROR)

        return create_result_response(
            token=token,
            result=ProcessingJobSerializer(
                instance=job,
                context={"request": request},
            ).data,
        )

    @extend_schema(
        responses={
            status.HTTP_200_OK: GenericResultResponseSerializer,
            status.HTTP_400_BAD_REQUEST: GenericResponseSerializer,
            status.HTTP_404_NOT_FOUND: GenericResponseSerializer,
            status.HTTP_500_INTERNAL_SERVER_ERROR: GenericResponseSerializer,
        },
        tags=["Data processing endpoints"],
    )
    @action(methods=["post"], detail=True, url_path="pause")
    def pause(self, request, pk=None):
        return self._change_state(request, pk, pause_job)

    @extend_schema(
        responses={
            status.HTTP_200_OK: GenericResultResponseSerializer,
            status.HTTP_400_BAD_REQUEST: GenericResponseSerializer,
            status.HTTP_404_NOT_FOUND: GenericResponseSerializer,
            status.HTTP_500_INTERNAL_SERVER_ERROR: GenericResponseSerializer,
        },
        tags=["Data processing endpoints"],
    )
    @action(methods=["post"], detail=True, url_path="resume")
    def resume(self, request, pk=None):
        return self._change_state(request, pk, resume_job)

    @extend_schema(
        responses={
            status.HTTP_200_OK: GenericResultResponseSerializer,
            status.HTTP_400_BAD_REQUEST: GenericResponseSerializer,
            status.HTTP_404_NOT_FOUND: GenericResponseSerializer,
            status.HTTP_500_INTERNAL_SERVER_ERROR: GenericResponseSerializer,
        },
        tags=["Data processing endpoints"],
    )
    @action(methods=["post"], detail=True, url_path="cancel")
    def cancel(self, request, pk=None):
        return self._change_state(request, pk, cancel_job)

    @extend_schema(
        responses={
            status.HTTP_200_OK: GenericResultResponseSerializer,
            status.HTTP_400_BAD_REQUEST: GenericResponseSerializer,
            status.HTTP_404_NOT_FOUND: GenericResponseSerializer,
            status.HTTP_500_INTERNAL_SERVER_ERROR: GenericResponseSerializer,
        },
        tags=["Data processing endpoints"],
        summary="Discard failed batches and finish the pipeline with partial input.",
    )
    @action(methods=["post"], detail=True, url_path="discard-failed")
    def discard_failed(self, request, pk=None):
        return self._change_state(request, pk, discard_failed_job)

    @extend_schema(
        responses={
            status.HTTP_200_OK: GenericResultResponseSerializer,
            status.HTTP_400_BAD_REQUEST: GenericResponseSerializer,
            status.HTTP_404_NOT_FOUND: GenericResponseSerializer,
            status.HTTP_500_INTERNAL_SERVER_ERROR: GenericResponseSerializer,
        },
        tags=["Data processing endpoints"],
    )
    @action(methods=["post"], detail=True, url_path="retry-failed")
    def retry_failed(self, request, pk=None):
        return self._change_state(request, pk, retry_failed_job)

    @extend_schema(
        responses={status.HTTP_200_OK: GenericResultResponseSerializer},
        tags=["Data processing endpoints"],
        summary="Retry recoverable failed batches in this Job and its descendants.",
    )
    @action(methods=["post"], detail=True, url_path="retry-pending")
    def retry_pending(self, request, pk=None):
        return self._change_state(request, pk, retry_failed_job_tree)


class ProcessingCapacityViewSet(viewsets.ViewSet):
    """Small root-level endpoint so dashboards need not know a Job id."""

    throttle_scope = "staff_api"
    permission_classes = [IsAdminUser]

    @extend_schema(tags=["Data processing endpoints"])
    def list(self, request):
        return Response(processing_capacity())
