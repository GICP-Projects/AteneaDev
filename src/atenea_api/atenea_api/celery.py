from celery import Celery

# ======================================================
# =====           CELERY GLOBAL VARIABLES          =====     
# ======================================================
REDIS_MAX_DATA_MB = 500 # 512MB https://stackoverflow.com/a/43210008

# ======================================================
# =====           CELERY CONFIGURATION             =====     
# ======================================================

app = Celery("ateneaQ")
# Using a string here means the worker doesn't have to serialize
# the configuration object to child processes.
# - namespace='CELERY' means all celery-related configuration keys
#   should have a `CELERY_` prefix.
app.config_from_object("django.conf:settings", namespace="CELERY")

# Load task modules from all registered Django app configs.
app.autodiscover_tasks()

@app.task(bind=True)
def debug_task(self):
    print(f"Request: {self.request!r}")


""" TODO CELERY BEAT
app.conf.beat_schedule = {
    "scheduled_load_all_rss": {
        "task": "load_all_rss",
        "schedule": crontab(minute=0, hour=8),  # every day at 8 a.m.
    },
}
app.conf.timezone = "UTC"
"""
