from rest_framework.routers import SimpleRouter
from app_scheduler.views import SchedulerStaffView

scheduler_router = SimpleRouter(trailing_slash=False)
scheduler_router.register(r"task", SchedulerStaffView, basename="scheduler")