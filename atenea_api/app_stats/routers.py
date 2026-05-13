from rest_framework.routers import SimpleRouter
from .views import MessageStatsViewSet

stats_router = SimpleRouter(trailing_slash=False)
stats_router.register(r"msg", MessageStatsViewSet, basename="message-stats")
