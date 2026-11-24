from rest_framework.routers import SimpleRouter
from app_scheduler.views import (
    ProcessingCapacityViewSet,
    ProcessingJobViewSet,
    ProcessingTriggerViewSet,
)

scheduler_router = SimpleRouter(trailing_slash=False)
scheduler_router.register(r"triggers", ProcessingTriggerViewSet, basename="processing-trigger")
scheduler_router.register(r"jobs", ProcessingJobViewSet, basename="processing-job")
scheduler_router.register(r"processing-capacity", ProcessingCapacityViewSet, basename="processing-capacity")
