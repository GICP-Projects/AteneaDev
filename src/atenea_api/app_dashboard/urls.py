from django.urls import path
from django.views.i18n import set_language

from app_dashboard import views


app_name = "app_dashboard"

urlpatterns = [
    path("login/", views.DashboardLoginView.as_view(), name="login"),
    path("logout/", views.DashboardLogoutView.as_view(), name="logout"),
    path("language/", set_language, name="language"),
    path("lookup/<str:kind>/", views.DashboardLookupView.as_view(), name="lookup"),
    path("search/count/", views.DashboardMessageSearchCountView.as_view(), name="search-count"),
    path("search/", views.DashboardMessageSearchView.as_view(), name="search"),
    path("stats/", views.DashboardStatisticsDataView.as_view(), name="statistics"),
    path("entities/", views.DashboardEntityRankingDataView.as_view(), name="entities"),
    path("", views.DashboardOverviewView.as_view(), name="home"),
    path("jobs/", views.JobListView.as_view(), name="jobs"),
    path("jobs/new/", views.JobCreateView.as_view(), name="job-new"),
    path("jobs/retry-processing/", views.RetryMessageProcessingView.as_view(), name="retry-message-processing"),
    path("jobs/<uuid:pk>/", views.JobDetailView.as_view(), name="job-detail"),
    path("jobs/<uuid:pk>/live/", views.JobLiveFragmentView.as_view(), name="job-live"),
    path("jobs/<uuid:pk>/graph/", views.JobGraphFragmentView.as_view(), name="job-graph"),
    path("jobs/<uuid:pk>/batches/", views.JobBatchDetailView.as_view(), name="job-batch-detail"),
    path("jobs/<uuid:pk>/<str:action>/", views.JobActionView.as_view(), name="job-action"),
    path("schedules/", views.ScheduleListView.as_view(), name="schedules"),
    path("schedules/new/", views.ScheduleFormView.as_view(), name="schedule-new"),
    path("schedules/<int:pk>/edit/", views.ScheduleFormView.as_view(), name="schedule-edit"),
    path("schedules/<int:pk>/toggle/", views.ScheduleToggleView.as_view(), name="schedule-toggle"),
    path("schedules/<int:pk>/delete/", views.ScheduleDeleteView.as_view(), name="schedule-delete"),
]
