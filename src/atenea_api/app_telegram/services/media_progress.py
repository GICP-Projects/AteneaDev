from datetime import datetime, timezone

import redis
from django.conf import settings
from app_telegram.enums import MediaProgressStatus


PROGRESS_KEY_PREFIX = "media_download"


def _client():
    return redis.Redis.from_url(settings.CELERY_BROKER_URL, decode_responses=True)


def _token_value(token):
    if hasattr(token, "hex"):
        return token.hex
    return str(token).replace("-", "")


def _key(token):
    return f"{PROGRESS_KEY_PREFIX}:{_token_value(token)}"


def _now():
    return datetime.now(timezone.utc).isoformat()


def init_progress(token, total=0, total_chunks=0):
    if not token:
        return
    key = _key(token)
    client = _client()
    client.hset(
        key,
        mapping={
            "token": str(token),
            "status": MediaProgressStatus.QUEUED.value,
            "total": int(total or 0),
            "total_chunks": int(total_chunks or 0),
            "processed": 0,
            "downloaded": 0,
            "skipped": 0,
            "failed": 0,
            "chunks_completed": 0,
            "chunks_scheduled": 0,
            "messages_scheduled": 0,
            "schedule_completed": 0,
            "started_at": _now(),
            "updated_at": _now(),
            "finished_at": "",
            "scheduler_started_at": "",
            "scheduler_finished_at": "",
        },
    )
    client.expire(key, settings.MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS)


def mark_scheduling(token):
    if not token:
        return
    key = _key(token)
    client = _client()
    client.hset(
        key,
        mapping={
            "status": MediaProgressStatus.SCHEDULING.value,
            "scheduler_started_at": _now(),
            "updated_at": _now(),
        },
    )
    client.expire(key, settings.MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS)


def set_schedule_total(token, total=0):
    if not token:
        return
    key = _key(token)
    client = _client()
    client.hset(key, mapping={"total": int(total or 0), "updated_at": _now()})
    client.expire(key, settings.MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS)


def increment_scheduled(token, chunks=0, messages=0):
    if not token:
        return
    key = _key(token)
    client = _client()
    pipe = client.pipeline()
    if chunks:
        pipe.hincrby(key, "chunks_scheduled", int(chunks))
        pipe.hincrby(key, "total_chunks", int(chunks))
    if messages:
        pipe.hincrby(key, "messages_scheduled", int(messages))
    pipe.hset(key, "updated_at", _now())
    pipe.expire(key, settings.MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS)
    pipe.execute()


def mark_schedule_completed(token):
    if not token:
        return
    key = _key(token)
    client = _client()
    data = client.hgetall(key)
    total_chunks = int(data.get("total_chunks") or 0)
    chunks_completed = int(data.get("chunks_completed") or 0)
    status = (
        MediaProgressStatus.COMPLETED if chunks_completed >= total_chunks
        else MediaProgressStatus.RUNNING
    )
    finished_at = _now() if status == MediaProgressStatus.COMPLETED else data.get("finished_at", "")
    client.hset(
        key,
        mapping={
            "status": status.value,
            "schedule_completed": 1,
            "scheduler_finished_at": _now(),
            "updated_at": _now(),
            "finished_at": finished_at,
        },
    )
    client.expire(key, settings.MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS)


def mark_running(token):
    if not token:
        return
    key = _key(token)
    client = _client()
    data = client.hgetall(key)
    status = (
        MediaProgressStatus.SCHEDULING
        if data.get("schedule_completed") == "0" and data.get("status") in {
            MediaProgressStatus.QUEUED, MediaProgressStatus.SCHEDULING,
        }
        else MediaProgressStatus.RUNNING
    )
    client.hset(key, mapping={"status": status.value, "updated_at": _now()})
    client.expire(key, settings.MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS)


def increment_progress(token, processed=0, downloaded=0, skipped=0, failed=0):
    if not token:
        return
    key = _key(token)
    client = _client()
    pipe = client.pipeline()
    if processed:
        pipe.hincrby(key, "processed", int(processed))
    if downloaded:
        pipe.hincrby(key, "downloaded", int(downloaded))
    if skipped:
        pipe.hincrby(key, "skipped", int(skipped))
    if failed:
        pipe.hincrby(key, "failed", int(failed))
    pipe.hset(key, "updated_at", _now())
    pipe.expire(key, settings.MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS)
    pipe.execute()


def complete_chunk(token):
    if not token:
        return
    key = _key(token)
    client = _client()
    chunks_completed = client.hincrby(key, "chunks_completed", 1)
    data = client.hgetall(key)
    total_chunks = int(data.get("total_chunks") or 0)
    schedule_completed = data.get("schedule_completed", "1") == "1"
    if schedule_completed and total_chunks and chunks_completed >= total_chunks:
        status = MediaProgressStatus.COMPLETED
        finished_at = _now()
    else:
        status = MediaProgressStatus.RUNNING
        finished_at = data.get("finished_at", "")
    client.hset(
        key,
        mapping={
            "status": status.value,
            "updated_at": _now(),
            "finished_at": finished_at,
        },
    )
    client.expire(key, settings.MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS)


def mark_failed(token, reason=""):
    if not token:
        return
    key = _key(token)
    client = _client()
    client.hset(
        key,
        mapping={
            "status": MediaProgressStatus.FAILED.value,
            "reason": reason,
            "updated_at": _now(),
            "finished_at": _now(),
        },
    )
    client.expire(key, settings.MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS)


def mark_completed(token):
    if not token:
        return
    key = _key(token)
    client = _client()
    client.hset(
        key,
        mapping={
            "status": MediaProgressStatus.COMPLETED.value,
            "updated_at": _now(),
            "finished_at": _now(),
        },
    )
    client.expire(key, settings.MEDIA_DOWNLOAD_PROGRESS_TTL_SECONDS)


def get_progress(token):
    data = _client().hgetall(_key(token))
    if not data:
        return None
    int_fields = [
        "total",
        "total_chunks",
        "processed",
        "downloaded",
        "skipped",
        "failed",
        "chunks_completed",
        "chunks_scheduled",
        "messages_scheduled",
    ]
    for field in int_fields:
        data[field] = int(data.get(field) or 0)
    data["schedule_completed"] = data.get("schedule_completed", "1") == "1"
    total = data["total"]
    data["percent"] = round((data["processed"] / total) * 100, 2) if total else 0.0
    data["scheduling_percent"] = (
        round((data["messages_scheduled"] / total) * 100, 2) if total else 0.0
    )
    return data
