from django.contrib import admin
from django.urls import path, include
from app_frontend.routers import front_router
from app_telegram.routers import telegram_router, telegram_search_router
from app_metadata.routers import metadata_router
from app_scheduler.routers import scheduler_router
from app_stats.routers import stats_router
from drf_spectacular.views import (
    SpectacularAPIView,
    SpectacularRedocView,
    SpectacularSwaggerView,
    SpectacularJSONAPIView,
)


API_V1_ROOT = "api/v1/"

# API endpoints - v1
endpoints_urls = [
    # app_users
    #path(API_V1_ROOT + "user/", include(users_router.urls)),
    # app_frontend
    path(API_V1_ROOT + "front/", include(front_router.urls)),
    # app_entity
    #path(API_V1_ROOT, include(staff_router_entity.urls)),
    # app_metadata
    path(API_V1_ROOT + "metadata/", include(metadata_router.urls)),
    # app_scheduler
    path(API_V1_ROOT + "scheduler/", include(scheduler_router.urls)),
    # app_telegram
    path(API_V1_ROOT + "tg/", include(telegram_router.urls + telegram_search_router.urls)),
    # app_stats
    path(API_V1_ROOT + "stats/", include(stats_router.urls)),
]

doc_urls = [
    # SWAGGER DOCUMENTATION PAGES
    path("schema/", SpectacularAPIView.as_view(), name="schema"),
    path(
        "swagger",
        SpectacularSwaggerView.as_view(),#template_name="swagger-ui.html", url_name="schema"),
        name="schema-swagger-ui",
    ),
    path(
        "redoc/",
        SpectacularRedocView.as_view(url_name="schema"),
        name="schema-redoc",
    ),
    path(
        "json-api/",
        SpectacularJSONAPIView.as_view(),
        name="schema-json",
    ),
]

urlpatterns = [
    ### OAUTH2 authentication (namespace = DRFSO2_URL_NAMESPACE) to avoid 'drf' is not a registered namespace)
    path(API_V1_ROOT + 'oauth/', include('app_users.urls', namespace='drf_social_o2')),

    ### API URLS ###
    path('admin/', admin.site.urls)
] + endpoints_urls + doc_urls

