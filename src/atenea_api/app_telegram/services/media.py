import asyncio
import logging
import re
from itertools import islice

from celery import group
from celery.app import shared_task
from botocore.exceptions import ClientError
from django.conf import settings
from django.contrib.contenttypes.models import ContentType
from django.db import transaction
from django.db.models import Prefetch
from django.utils import timezone
from telethon import TelegramClient
from telethon.errors import AuthKeyError, FloodWaitError, RPCError
from telethon.sessions import StringSession
from telethon.tl.types import InputPeerChannel

from app_base.api import create_advance_filter
from app_entity.models import AnnotatedEntity, EntityItem
from app_telegram.models import (
    MessageItem,
    RoomItem,
    TelegramAuth,
    TelegramExternalUrlItem,
    TelegramMediaItem,
)
from app_telegram.serializers import ANY as TAG_ANY
from app_telegram.services.media_storage import (
    build_object_key,
    classify_media_risk,
    generate_download_url,
    guess_extension,
    is_whitelisted_download_domain,
    normalize_domain,
    normalize_extension,
    provider_from_domain,
    delete_object,
    sha256_bytes,
    upload_bytes,
)
from app_telegram.services.media_progress import (
    complete_chunk,
    get_progress,
    increment_scheduled,
    increment_progress,
    init_progress,
    mark_failed,
    mark_running,
    mark_schedule_completed,
    mark_scheduling,
    set_schedule_total,
)


logger = logging.getLogger(__name__)

DOWNLOADABLE_MEDIA_TYPES = [
    MessageItem.PHOTO,
    MessageItem.VIDEO,
    MessageItem.AUDIO,
    MessageItem.DOC,
    MessageItem.GIF,
]

URL_RE = re.compile(r"https?://[^\s)\]>\"']+", re.IGNORECASE)


def _chunked(iterable, size):
    iterator = iter(iterable)
    while chunk := list(islice(iterator, size)):
        yield chunk


def _normalize_extensions(extensions):
    return [normalize_extension(ext) for ext in (extensions or []) if normalize_extension(ext)]


def _message_filter(
    room=None,
    tags=None,
    tag_match=TAG_ANY,
    lang=None,
    createdat_min=None,
    createdat_max=None,
    stored_since=None,
    is_reply=None,
):
    return create_advance_filter(
        and_filter_fields={
            "created_at__gte": createdat_min,
            "created_at__lte": createdat_max,
            "stored_date__gte": stored_since,
            "lang__in": lang or [],
            "is_reply": is_reply,
        },
        list_filter_fields={
            "room__unique_name__iexact": {
                "values": [r.strip() for r in (room or [])],
                "OR": True,
            },
            "room__tags__icontains": {
                "values": tags or [],
                "OR": tag_match == TAG_ANY,
            },
        },
    )


def get_filtered_messages_queryset(**filters):
    return (
        MessageItem.objects.filter(_message_filter(**filters))
        .select_related("room", "room__access_auth")
        .order_by("pk")
    )


def media_download_pipeline(
    token=None,
    room=None,
    tags=None,
    tag_match=TAG_ANY,
    lang=None,
    createdat_min=None,
    createdat_max=None,
    stored_since=None,
    is_reply=None,
    extensions=None,
    max_file_size_bytes=None,
    metadata_only=False,
    force=False,
):
    init_progress(token, total=0, total_chunks=0)
    schedule_telegram_media_download.apply_async(
        kwargs={
            "token": str(token) if token else None,
            "room": room,
            "tags": tags,
            "tag_match": tag_match,
            "lang": lang,
            "createdat_min": createdat_min,
            "createdat_max": createdat_max,
            "stored_since": stored_since,
            "is_reply": is_reply,
            "extensions": extensions,
            "max_file_size_bytes": max_file_size_bytes,
            "metadata_only": metadata_only,
            "force": force,
        }
    )
    logger.info(
        "Queued Telegram media download scheduler. token=%s force=%s extensions=%s max_file_size_bytes=%s metadata_only=%s",
        token,
        force,
        extensions or [],
        max_file_size_bytes or settings.MEDIA_S3["max_file_size_bytes"],
        metadata_only,
    )
    return None


@shared_task(track_started=True, ignore_result=False)
def schedule_telegram_media_download(
    token=None,
    room=None,
    tags=None,
    tag_match=TAG_ANY,
    lang=None,
    createdat_min=None,
    createdat_max=None,
    stored_since=None,
    is_reply=None,
    extensions=None,
    max_file_size_bytes=None,
    metadata_only=False,
    force=False,
):
    mark_scheduling(token)
    queryset = get_filtered_messages_queryset(
        room=room,
        tags=tags,
        tag_match=tag_match,
        lang=lang,
        createdat_min=createdat_min,
        createdat_max=createdat_max,
        stored_since=stored_since,
        is_reply=is_reply,
    ).filter(
        media_type__in=DOWNLOADABLE_MEDIA_TYPES,
        room__access_auth__isnull=False,
        room__access_hash__isnull=False,
        room__is_valid=True,
        room__is_deleted=False,
    )
    if not force:
        queryset = queryset.exclude(downloaded_media__status=TelegramMediaItem.DOWNLOADED)

    total_items = queryset.count()
    set_schedule_total(token, total_items)
    logger.info(
        "Telegram media download request matched %s messages. token=%s force=%s extensions=%s max_file_size_bytes=%s metadata_only=%s",
        total_items,
        token,
        force,
        extensions or [],
        max_file_size_bytes or settings.MEDIA_S3["max_file_size_bytes"],
        metadata_only,
    )
    if not total_items:
        mark_schedule_completed(token)
        return 0

    tasks_scheduled = 0
    for auth in TelegramAuth.objects.filter(is_valid=True).only("pk", "name", "api_id", "api_hash", "session"):
        auth_queryset = queryset.filter(room__access_auth=auth)
        pk_iterator = auth_queryset.values_list("pk", flat=True).iterator(chunk_size=1000)
        for chunk in _chunked(pk_iterator, 1000):
            increment_scheduled(token, chunks=1, messages=len(chunk))
            download_telegram_media.apply_async(
                args=[
                    [str(pk) for pk in chunk],
                    {
                        "pk": str(auth.pk),
                        "name": auth.name,
                        "api_id": auth.api_id,
                        "api_hash": auth.api_hash,
                        "session": auth.session,
                    },
                ],
                kwargs={
                    "extensions": _normalize_extensions(extensions),
                    "max_file_size_bytes": max_file_size_bytes or settings.MEDIA_S3["max_file_size_bytes"],
                    "metadata_only": metadata_only,
                    "force": force,
                    "token": token,
                },
            )
            tasks_scheduled += 1

    if tasks_scheduled:
        mark_schedule_completed(token)
        logger.info("Queued %s Telegram media download tasks. token=%s", tasks_scheduled, token)
    else:
        logger.warning(
            "No valid Telegram auth was available for %s matched media messages. token=%s",
            total_items,
            token,
        )
        mark_failed(token, "no_valid_telegram_auth_for_filtered_messages")
    return total_items


@shared_task(track_started=True, ignore_result=False)
def download_telegram_media(
    messages_pk,
    auth_key,
    extensions=None,
    max_file_size_bytes=None,
    metadata_only=False,
    force=False,
    token=None,
):
    messages = list(
        MessageItem.objects.filter(pk__in=messages_pk)
        .select_related("room")
        .order_by("pk")
    )
    max_size_bytes = int(max_file_size_bytes or settings.MEDIA_S3["max_file_size_bytes"])
    extensions = _normalize_extensions(extensions)
    mark_running(token)

    async def _download_all():
        results = []
        async with TelegramClient(
            StringSession(auth_key.get("session", "")),
            auth_key.get("api_id", 0),
            auth_key.get("api_hash", ""),
        ) as client:
            await client.start()
            for item in messages:
                results.append(await _download_one(client, item))
        return results

    async def _download_one(client, item):
        base = {
            "message_id": item.pk,
            "room_id": item.room_id,
            "status": TelegramMediaItem.FAILED,
            "reason": "",
            "bucket": settings.MEDIA_S3["bucket"],
            "object_key": "",
            "original_file_name": "",
            "extension": "",
            "mime_type": "",
            "size_bytes": None,
            "sha256": "",
            "risk_level": TelegramMediaItem.RISK_UNKNOWN,
            "risk_reason": "",
            "is_potentially_dangerous": False,
            "downloaded_at": None,
        }
        try:
            input_room = InputPeerChannel(item.room.tg_id, item.room.access_hash)
            tg_msg = await client.get_messages(input_room, ids=item.msg_id)
            if not tg_msg or not getattr(tg_msg, "media", None):
                logger.warning(
                    "Telegram media not found for stored message. message_id=%s room_id=%s telegram_msg_id=%s",
                    item.pk,
                    item.room_id,
                    item.msg_id,
                )
                return {**base, "status": TelegramMediaItem.SKIPPED, "reason": "no_media"}

            file_obj = getattr(tg_msg, "file", None)
            file_name = (getattr(file_obj, "name", None) or f"message_{item.msg_id}").strip()
            mime_type = getattr(file_obj, "mime_type", "") or ""
            size_bytes = getattr(file_obj, "size", None)
            extension = normalize_extension(getattr(file_obj, "ext", "") or guess_extension(file_name, mime_type))
            if not extension and getattr(tg_msg, "photo", None):
                extension = ".jpg"
                file_name = file_name if "." in file_name else f"{file_name}.jpg"

            if extensions and extension not in extensions:
                return {
                    **base,
                    "status": TelegramMediaItem.SKIPPED,
                    "reason": "extension_filtered",
                    "original_file_name": file_name,
                    "extension": extension,
                    "mime_type": mime_type,
                    "size_bytes": size_bytes,
                }

            if metadata_only:
                risk = classify_media_risk(extension, mime_type)
                return {
                    **base,
                    "status": TelegramMediaItem.SKIPPED,
                    "reason": "metadata_only",
                    "original_file_name": file_name,
                    "extension": extension,
                    "mime_type": mime_type,
                    "size_bytes": size_bytes,
                    "risk_level": risk.level,
                    "risk_reason": risk.reason,
                    "is_potentially_dangerous": risk.potentially_dangerous,
                }

            if size_bytes and size_bytes > max_size_bytes:
                return {
                    **base,
                    "status": TelegramMediaItem.SKIPPED,
                    "reason": "file_too_large",
                    "original_file_name": file_name,
                    "extension": extension,
                    "mime_type": mime_type,
                    "size_bytes": size_bytes,
                }

            data = await tg_msg.download_media(file=bytes)
            if not data:
                logger.warning(
                    "Telegram returned empty media bytes. message_id=%s room_id=%s telegram_msg_id=%s",
                    item.pk,
                    item.room_id,
                    item.msg_id,
                )
                return {**base, "status": TelegramMediaItem.FAILED, "reason": "telegram_download_failed"}
            if len(data) > max_size_bytes:
                return {
                    **base,
                    "status": TelegramMediaItem.SKIPPED,
                    "reason": "file_too_large",
                    "original_file_name": file_name,
                    "extension": extension,
                    "mime_type": mime_type,
                    "size_bytes": len(data),
                }

            digest = sha256_bytes(data)
            object_key = build_object_key(item.room.unique_name or item.room.title, item.msg_id, file_name, digest)
            upload_bytes(object_key, data, mime_type)
            risk = classify_media_risk(extension, mime_type)
            return {
                **base,
                "status": TelegramMediaItem.DOWNLOADED,
                "reason": "",
                "object_key": object_key,
                "original_file_name": file_name,
                "extension": extension,
                "mime_type": mime_type,
                "size_bytes": len(data),
                "sha256": digest,
                "risk_level": risk.level,
                "risk_reason": risk.reason,
                "is_potentially_dangerous": risk.potentially_dangerous,
                "downloaded_at": timezone.now(),
            }
        except FloodWaitError as exc:
            logger.warning(
                "Telegram flood wait while downloading media. message_id=%s room_id=%s telegram_msg_id=%s seconds=%s",
                item.pk,
                item.room_id,
                item.msg_id,
                exc.seconds,
            )
            return {**base, "status": TelegramMediaItem.FAILED, "reason": f"flood_wait_{exc.seconds}"}
        except RPCError as exc:
            logger.warning(
                "Telegram RPC error while downloading media. message_id=%s room_id=%s telegram_msg_id=%s error=%s",
                item.pk,
                item.room_id,
                item.msg_id,
                exc.__class__.__name__,
            )
            return {**base, "status": TelegramMediaItem.FAILED, "reason": exc.__class__.__name__}
        except Exception as exc:
            logger.exception("Unexpected media download error for message %s", item.pk)
            return {**base, "status": TelegramMediaItem.FAILED, "reason": exc.__class__.__name__}

    try:
        results = asyncio.run(_download_all())
    except (ValueError, RuntimeError, AuthKeyError, EOFError) as exc:
        TelegramAuth.objects.filter(pk=auth_key.get("pk")).update(is_valid=False)
        logger.error(
            "Invalid Telegram auth while downloading media. auth=%s token=%s messages=%s error=%s",
            auth_key.get("name"),
            token,
            len(messages),
            exc,
        )
        increment_progress(token, processed=len(messages), failed=len(messages))
        complete_chunk(token)
        return 0

    with transaction.atomic():
        for result in results:
            message_id = result.pop("message_id")
            TelegramMediaItem.objects.update_or_create(
                message_id=message_id,
                defaults=result,
            )
    downloaded = sum(1 for result in results if result["status"] == TelegramMediaItem.DOWNLOADED)
    skipped = sum(1 for result in results if result["status"] == TelegramMediaItem.SKIPPED)
    failed = sum(1 for result in results if result["status"] == TelegramMediaItem.FAILED)
    increment_progress(
        token,
        processed=len(results),
        downloaded=downloaded,
        skipped=skipped,
        failed=failed,
    )
    complete_chunk(token)
    logger.info(
        "Finished Telegram media download chunk. token=%s auth=%s total=%s downloaded=%s skipped=%s failed=%s metadata_only=%s",
        token,
        auth_key.get("name"),
        len(results),
        downloaded,
        skipped,
        failed,
        metadata_only,
    )
    return len(results)


def get_media_download_progress(token):
    return get_progress(token)


def external_url_collect_pipeline(
    token=None,
    room=None,
    tags=None,
    tag_match=TAG_ANY,
    lang=None,
    createdat_min=None,
    createdat_max=None,
    stored_since=None,
    is_reply=None,
    use_text_fallback=True,
):
    queryset = get_filtered_messages_queryset(
        room=room,
        tags=tags,
        tag_match=tag_match,
        lang=lang,
        createdat_min=createdat_min,
        createdat_max=createdat_max,
        stored_since=stored_since,
        is_reply=is_reply,
    )
    total_items = queryset.count()
    logger.info(
        "External URL collection request matched %s messages. token=%s use_text_fallback=%s",
        total_items,
        token,
        use_text_fallback,
    )
    if not total_items:
        return 0

    tasks = []
    pk_iterator = queryset.values_list("pk", flat=True).iterator(chunk_size=5000)
    for chunk in _chunked(pk_iterator, 5000):
        tasks.append(collect_external_urls.s([str(pk) for pk in chunk], use_text_fallback=use_text_fallback))
    logger.info("Queued %s external URL collection tasks. token=%s", len(tasks), token)
    group(tasks).apply_async()
    return total_items


@shared_task(track_started=True, ignore_result=False)
def collect_external_urls(messages_pk, use_text_fallback=True):
    messages = list(
        MessageItem.objects.filter(pk__in=messages_pk)
        .only("id", "room_id", "text")
    )
    msg_by_pk = {msg.pk: msg for msg in messages}
    urls_by_msg = {msg.pk: set() for msg in messages}

    msg_content_type = ContentType.objects.get_for_model(MessageItem)
    for row in (
        AnnotatedEntity.objects.filter(
            content_type=msg_content_type,
            object_id__in=list(msg_by_pk.keys()),
            entity__type=EntityItem.URL,
        )
        .select_related("entity")
        .values("object_id", "entity__name")
    ):
        urls_by_msg[row["object_id"]].add(row["entity__name"])

    if use_text_fallback:
        for msg in messages:
            for url in URL_RE.findall(msg.text or ""):
                urls_by_msg[msg.pk].add(url)

    now = timezone.now()
    items = []
    for msg in messages:
        for url in urls_by_msg[msg.pk]:
            domain = normalize_domain(url)
            if not is_whitelisted_download_domain(domain):
                continue
            items.append(
                TelegramExternalUrlItem(
                    message_id=msg.pk,
                    room_id=msg.room_id,
                    url=url,
                    domain=domain,
                    provider=provider_from_domain(domain),
                    status=TelegramExternalUrlItem.NOT_DOWNLOADED,
                    last_seen_at=now,
                )
            )
    if not items:
        logger.info(
            "Finished external URL collection chunk. messages=%s whitelisted_urls=0 stored=0",
            len(messages),
        )
        return 0

    TelegramExternalUrlItem.objects.bulk_create(
        items,
        update_conflicts=True,
        unique_fields=["message", "url"],
        update_fields=["domain", "provider", "status", "last_seen_at"],
        batch_size=settings.BULK_BATCH_SIZE,
    )
    logger.info(
        "Finished external URL collection chunk. messages=%s whitelisted_urls=%s stored=%s",
        len(messages),
        len(items),
        len(items),
    )
    return len(items)


def delete_downloadable_items(
    room=None,
    tags=None,
    tag_match=TAG_ANY,
    createdat_min=None,
    createdat_max=None,
    ext=None,
    min_size_bytes=None,
    dry_run=False,
    confirm=False,
):
    normalized_extensions = set(_normalize_extensions(ext))
    message_qs = get_filtered_messages_queryset(
        room=room,
        tags=tags,
        tag_match=tag_match,
        createdat_min=createdat_min,
        createdat_max=createdat_max,
    )
    result = {
        "media_matched": 0,
        "media_marked_deleted": 0,
        "s3_objects_matched": 0,
        "s3_objects_deleted": 0,
        "s3_delete_failed": 0,
        "dry_run": dry_run,
        "confirm": confirm,
        "confirm_threshold": settings.MEDIA_DOWNLOADABLE_DELETE_CONFIRM_THRESHOLD,
        "requires_confirmation": False,
    }

    media_qs = TelegramMediaItem.objects.filter(
        message__in=message_qs,
        status=TelegramMediaItem.DOWNLOADED,
    )
    if normalized_extensions:
        media_qs = media_qs.filter(extension__in=normalized_extensions)
    if min_size_bytes is not None:
        media_qs = media_qs.filter(size_bytes__gte=min_size_bytes)

    result["media_matched"] = media_qs.count()
    result["s3_objects_matched"] = media_qs.exclude(object_key="").count()
    result["requires_confirmation"] = (
        result["media_matched"] > settings.MEDIA_DOWNLOADABLE_DELETE_CONFIRM_THRESHOLD
    )
    if (
        not dry_run
        and not confirm
        and result["requires_confirmation"]
    ):
        logger.warning(
            "Bucket media delete request requires confirmation. media_matched=%s threshold=%s",
            result["media_matched"],
            settings.MEDIA_DOWNLOADABLE_DELETE_CONFIRM_THRESHOLD,
        )
        return result

    for media in media_qs.iterator(chunk_size=500):
        if dry_run:
            continue
        object_deleted = False
        if media.object_key:
            try:
                delete_object(media.object_key)
                object_deleted = True
                result["s3_objects_deleted"] += 1
            except ClientError as exc:
                error_code = exc.response.get("Error", {}).get("Code")
                if error_code in {"NoSuchKey", "404", "NotFound"}:
                    object_deleted = True
                    logger.warning(
                        "S3 object was already missing while deleting Telegram media. media_id=%s object_key=%s error_code=%s",
                        media.pk,
                        media.object_key,
                        error_code,
                    )
                else:
                    result["s3_delete_failed"] += 1
                    logger.warning(
                        "Failed to delete S3 object for Telegram media. media_id=%s object_key=%s error=%s",
                        media.pk,
                        media.object_key,
                        exc,
                    )
            except Exception as exc:
                result["s3_delete_failed"] += 1
                logger.warning(
                    "Unexpected error deleting S3 object for Telegram media. media_id=%s object_key=%s error=%s",
                    media.pk,
                    media.object_key,
                    exc,
                )
        else:
            object_deleted = True

        if object_deleted:
            media.status = TelegramMediaItem.DELETED
            media.reason = "deleted_by_request"
            media.downloaded_at = None
            media.save(update_fields=["status", "reason", "downloaded_at", "updated_at"])
            result["media_marked_deleted"] += 1

    logger.info(
        "Finished bucket media delete request. dry_run=%s extensions=%s min_size_bytes=%s media_matched=%s media_deleted=%s s3_matched=%s s3_deleted=%s s3_failed=%s",
        dry_run,
        sorted(normalized_extensions),
        min_size_bytes,
        result["media_matched"],
        result["media_marked_deleted"],
        result["s3_objects_matched"],
        result["s3_objects_deleted"],
        result["s3_delete_failed"],
    )
    return result


def build_downloadable_catalog(
    room=None,
    tags=None,
    tag_match=TAG_ANY,
    lang=None,
    createdat_min=None,
    createdat_max=None,
    stored_since=None,
    is_reply=None,
    provider=None,
    ext=None,
    status=None,
    source=None,
    limit=100,
    max_items=100,
):
    rooms = {}
    source = source or "all"
    normalized_extensions = set(_normalize_extensions(ext))

    if source in ("all", "bucket"):
        media_qs = (
            TelegramMediaItem.objects.filter(
                message__in=get_filtered_messages_queryset(
                    room=room,
                    tags=tags,
                    tag_match=tag_match,
                    lang=lang,
                    createdat_min=createdat_min,
                    createdat_max=createdat_max,
                    stored_since=stored_since,
                    is_reply=is_reply,
                )
            )
            .select_related("room", "message")
            .order_by("room__unique_name", "-message__created_at")
        )
        if normalized_extensions:
            media_qs = media_qs.filter(extension__in=normalized_extensions)
        if status:
            media_qs = media_qs.filter(status=status)
        else:
            media_qs = media_qs.exclude(status=TelegramMediaItem.DELETED)
        for media in media_qs.iterator(chunk_size=1000):
            _append_room_item(rooms, media.room, _serialize_media_item(media), limit, max_items)

    if source in ("all", "external_url") and not normalized_extensions:
        urls_qs = (
            TelegramExternalUrlItem.objects.filter(
                message__in=get_filtered_messages_queryset(
                    room=room,
                    tags=tags,
                    tag_match=tag_match,
                    lang=lang,
                    createdat_min=createdat_min,
                    createdat_max=createdat_max,
                    stored_since=stored_since,
                    is_reply=is_reply,
                )
            )
            .select_related("room", "message")
            .order_by("room__unique_name", "-message__created_at")
        )
        if provider:
            urls_qs = urls_qs.filter(provider=provider)
        if status:
            urls_qs = urls_qs.filter(status=status)
        else:
            urls_qs = urls_qs.exclude(status=TelegramExternalUrlItem.DELETED)
        for external_url in urls_qs.iterator(chunk_size=1000):
            _append_room_item(rooms, external_url.room, _serialize_external_url_item(external_url), limit, max_items)

    return list(rooms.values())[:limit]


def _append_room_item(rooms, room, item, limit, max_items):
    if room.pk not in rooms:
        if len(rooms) >= limit:
            return
        rooms[room.pk] = {
            "room": {
                "id": str(room.pk),
                "unique_name": room.unique_name,
                "title": room.title,
                "link": room.link,
                "tags": room.tags,
            },
            "items": [],
        }
    if len(rooms[room.pk]["items"]) < max_items:
        rooms[room.pk]["items"].append(item)


def _serialize_media_item(media):
    return {
        "source": "bucket",
        "message_id": str(media.message_id),
        "telegram_msg_id": media.message.msg_id,
        "message_link": media.message.link,
        "created_at": media.message.created_at.isoformat() if media.message.created_at else None,
        "file_name": media.original_file_name,
        "extension": media.extension,
        "mime_type": media.mime_type,
        "size_bytes": media.size_bytes,
        "sha256": media.sha256,
        "status": media.status,
        "risk_level": media.risk_level,
        "risk_reason": media.risk_reason,
        "download_url": (
            generate_download_url(media.object_key, media.original_file_name)
            if media.status == TelegramMediaItem.DOWNLOADED and media.object_key
            else None
        ),
        "download_url_expires_in": settings.MEDIA_S3["presigned_ttl_seconds"],
    }


def _serialize_external_url_item(external_url):
    return {
        "source": "external_url",
        "message_id": str(external_url.message_id),
        "telegram_msg_id": external_url.message.msg_id,
        "message_link": external_url.message.link,
        "created_at": external_url.message.created_at.isoformat() if external_url.message.created_at else None,
        "provider": external_url.provider,
        "domain": external_url.domain,
        "url": external_url.url,
        "status": external_url.status,
    }
