"""Reconcile Telegram Rooms in PostgreSQL and synchronize their search metadata.

Scan and Populate reconcile identity and message URLs inside their checkpoint
transaction. A durable child Job then projects pending Rooms to Elasticsearch
and Qdrant by room_id. Index writers share an advisory lock for each Room.
"""

import hashlib
import re
import uuid
from contextlib import contextmanager
from dataclasses import dataclass
from datetime import datetime, timedelta

from billiard.exceptions import SoftTimeLimitExceeded
from django.conf import settings
from django.db import OperationalError, connections as db_connections, transaction
from django.db.models import CharField, F, Func, Value
from django.db.models.functions import Concat, Substr
from django.utils import timezone
from elasticsearch.dsl import connections

from app_scheduler.contracts import ProcessingSummary
from app_scheduler.models import ProcessingBatch
from app_scheduler.services.jobs import (
    StaleStageError, ensure_stage_lease_locked,
)
from app_telegram.job_keys import TELEGRAM_ROOM_METADATA_PROCESSOR, TELEGRAM_ROOM_METADATA_STAGE
from app_telegram.models import MessageItem, RoomItem
from app_telegram.services.jobs import ROOM_METADATA_SYNC_VERSION, room_metadata_stage_summary


MESSAGE_METADATA_FIELDS = frozenset({"unique_name", "about", "tags"})


# ======================================================
# =====       POSTGRESQL ROOM RECONCILIATION        =====
# ======================================================


class RoomReconciliationError(Exception):
    def __init__(self, reason, owner_ids=()):
        """Describe an identity or URL conflict that defers reconciliation.

        Parameters
        ----------
        reason : str
            Stable failure code recorded in Scan or Populate progress.
        owner_ids : iterable, optional
            Room primary keys whose current identity must be checked first.
        """
        self.reason = reason
        self.owner_ids = set(owner_ids)
        super().__init__(reason)


@dataclass(frozen=True)
class RoomIdentitySnapshot:
    """Reliable Telegram peer metadata with its local observation timestamp.

    about_observed distinguishes a basic message entity from a full response
    confirming the description, including a deliberately empty description.
    """

    tg_id: int
    username: str | None
    title: str
    observed_at: datetime
    about: str | None = None
    about_observed: bool = False

    @classmethod
    def from_entity(cls, entity, tg_id):
        """Capture available username and title when the Telegram peer ID matches.

        Parameters
        ----------
        entity : object or None
            Telegram chat entity, which may be absent or incomplete.
        tg_id : int
            Expected stable Telegram peer ID.

        Returns
        -------
        RoomIdentitySnapshot or None
            Identity with its observation time, or None if fields are unavailable.
            Reduced (min) entities are usable when these fields are present. The
            caller confirms a missing username by ID before treating it as private.
        """
        if getattr(entity, "id", None) != tg_id:
            return None
        if not hasattr(entity, "username") or getattr(entity, "title", None) is None:
            return None
        return cls(entity.id, entity.username, entity.title, timezone.now())


def room_identity_changed(room, snapshot):
    """Compare the public identity that can activate a scan reconciliation."""
    return (
        RoomItem.normalize_username(room.unique_name) != RoomItem.normalize_username(snapshot.username)
        or room.link != RoomItem.build_public_link(snapshot.username)
        or room.title != snapshot.title
    )


def rebase_messages(messages, link):
    """Replace in-memory message URL prefixes with the current Room link.

    Parameters
    ----------
    messages : iterable of MessageItem
        Unsaved messages whose URLs are updated in place.
    link : str
        Current canonical Room URL.

    Raises
    ------
    RoomReconciliationError
        A message URL lacks a post/comment suffix.

    Notes
    -----
    The entire suffix is preserved, including comment IDs and query parameters.
    This function performs no database writes.
    """
    for message in messages:
        parts = message.link.split("/", 4)
        if len(parts) != 5:
            raise RoomReconciliationError("message_link_invalid")
        message.link = f"{link.rstrip('/')}/{parts[4]}"


def conflicting_message_owners(messages):
    """Find historical Rooms occupying incoming message URLs.

    Parameters
    ----------
    messages : iterable of MessageItem
        Incoming messages to check against existing unique links.

    Returns
    -------
    set
        Primary keys of other Rooms currently owning the URLs.

    Raises
    ------
    RoomReconciliationError
        Incoming links repeat, or an existing link belongs to the same Room.

    Notes
    -----
    A renamed TSANoticias may still own t.me/thespanisharmy/77 when a different
    channel reuses thespanisharmy and publishes its own message 77. Return the
    historical owner so its old URL can be reconciled, even if the new channel's
    stored username is already correct.
    """
    messages = list(messages)
    incoming = {message.link: message.room_id for message in messages}
    if len(incoming) != len(messages):
        raise RoomReconciliationError("message_link_conflict")
    owners = set()
    for start in range(0, len(messages), 2500):
        links = [message.link for message in messages[start:start + 2500]]
        for link, room_id in MessageItem.objects.filter(link__in=links).values_list("link", "room_id"):
            if room_id == incoming[link]:
                raise RoomReconciliationError("message_link_conflict")
            owners.add(room_id)
    return owners


def _source_prefixes(room):
    """Find the Room's stored and historical message URL prefixes.

    Parameters
    ----------
    room : RoomItem
        Room whose message prefixes are being reconciled.

    Returns
    -------
    set of str
        Valid public prefixes preserving their original spelling, selected in
        PostgreSQL without loading message rows.

    Raises
    ------
    RoomReconciliationError
        A stored Room or message URL is not a supported public Telegram URL.
        Original URLs are checked before extracting prefixes, so private links
        such as t.me/c/123/456 cannot be mistaken for a public username.
    """
    messages = MessageItem.objects.filter(room_id=room.pk)
    room_prefix = room.link.rstrip("/")
    public_prefix = rf"https://t\.me/{RoomItem.USERNAME_PATTERN}"
    if not re.fullmatch(public_prefix, room_prefix) or messages.exclude(
        link__regex=rf"^{public_prefix}/[0-9]+([?].*)?$",
    ).exists():
        raise RoomReconciliationError("message_link_invalid")
    values = messages.annotate(
        name=Func(F("link"), Value("/"), Value(4), function="split_part", output_field=CharField())
    ).order_by().values_list("name", flat=True).distinct()
    return {f"https://t.me/{name}" for name in values} | {room_prefix}


def mark_room_metadata_pending(room, fields=None):
    """Accumulate changed fields and renew a locked Room's pending timestamp.

    Parameters
    ----------
    room : RoomItem
        Room locked by the caller in its checkpoint transaction. Its in-memory
        pending fields and timestamp are updated together with PostgreSQL.
    fields : iterable of str, optional
        Changed model fields. Omission requests a full synchronization for
        legacy work. An empty iterable creates no additional work.

    Returns
    -------
    datetime or None
        New pending timestamp, or the existing value when fields is empty.

    Raises
    ------
    RuntimeError
        No transaction is active. QuerySet writes avoid indexing signals.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Marking room metadata requires an atomic transaction")
    pending = set(MESSAGE_METADATA_FIELDS if fields is None else fields)
    if not pending:
        return room.metadata_sync_pending_at
    if room.metadata_sync_pending_at:
        # An empty legacy list means unknown work, so merging a newer change
        # must preserve the full synchronization that it still requires.
        pending.update(room.metadata_sync_pending_fields or MESSAGE_METADATA_FIELDS)
    stamp = timezone.now()
    if room.metadata_sync_pending_at and stamp <= room.metadata_sync_pending_at:
        stamp = room.metadata_sync_pending_at + timedelta(microseconds=1)
    fields = sorted(pending)
    RoomItem.objects.filter(pk=room.pk).update(
        metadata_sync_pending_at=stamp, metadata_sync_pending_fields=fields,
    )
    room.metadata_sync_pending_at = stamp
    room.metadata_sync_pending_fields = fields
    return stamp


def reconcile_room(room, snapshot, *, force=False):
    """Update one Room and its historical message URLs atomically.

    Parameters
    ----------
    room : RoomItem
        Room identified by its stable Telegram peer ID.
    snapshot : RoomIdentitySnapshot
        Reliable Telegram identity. Older observations are ignored.
    force : bool, default=False
        Check historical prefixes even when the Room username is already current.

    Returns
    -------
    tuple
        The locked, updated Room and string IDs requiring index synchronization.
        History records previous values for changed username, title and about
        fields; unchanged fields are null. Detection dates are observations.

    Raises
    ------
    RuntimeError
        The caller has not opened a transaction.
    RoomReconciliationError
        The peer ID, public username or destination links cannot be reconciled.
    OperationalError
        A required row lock cannot be acquired immediately.

    Notes
    -----
    The caller commits this work with messages, offsets and synchronization
    Handoffs. No search or vector requests run while these rows are locked.
    """
    if not transaction.get_connection().in_atomic_block:
        raise RuntimeError("Room reconciliation requires an atomic transaction")
    locked = RoomItem.objects.select_for_update(nowait=True, of=("self",)).get(pk=room.pk)
    if snapshot.tg_id != locked.tg_id:
        raise RoomReconciliationError("room_identity_mismatch")
    if locked.username_checked_at and snapshot.observed_at < locked.username_checked_at:
        return locked, {str(locked.pk)} if locked.metadata_sync_pending_at else set()
    try:
        target = RoomItem.build_public_link(snapshot.username)
    except ValueError as exc:
        raise RoomReconciliationError("room_username_missing") from exc
    previous = RoomItem.normalize_username(locked.unique_name)
    renamed = previous != RoomItem.normalize_username(snapshot.username)
    title_changed = locked.title != snapshot.title
    identity_changed = renamed or locked.link != target or title_changed
    if identity_changed and not snapshot.about_observed:
        raise RoomReconciliationError("room_about_unavailable")
    about_changed = snapshot.about_observed and (locked.about or "") != (snapshot.about or "")
    changed_fields = set()
    if renamed:
        changed_fields.add("unique_name")
    if locked.link != target:
        changed_fields.add("link")
    if title_changed:
        changed_fields.add("title")
    if about_changed:
        changed_fields.add("about")
    sources = _source_prefixes(locked) if force or renamed or locked.link != target else set()
    owner_ids = set()
    if locked.link != target:
        owner_ids.update(RoomItem.objects.filter(link=target).exclude(pk=locked.pk).values_list("pk", flat=True))
    updates = []
    for prefix in sorted(sources - {target}):
        source = MessageItem.objects.filter(room_id=locked.pk, link__startswith=prefix + "/")
        expression = Concat(Value(target + "/"), Substr(F("link"), len(prefix) + 2))
        targets = source.annotate(target_link=expression).values("target_link")
        collisions = MessageItem.objects.filter(link__in=targets).exclude(pk__in=source.values("pk"))
        owner_ids.update(collisions.values_list("room_id", flat=True))
        updates.append((prefix, source, expression))
    if owner_ids:
        raise RoomReconciliationError("room_username_conflict", owner_ids)
    # Comments lock their parent before locking Room. NOWAIT avoids the inverse
    # wait cycle while a rename updates those same parent rows.
    for _, source, _ in updates:
        for _ in source.select_for_update(nowait=True, of=("self",)).order_by("pk").values_list("pk", flat=True).iterator(chunk_size=2500):
            pass
    history = list(locked.username_history)
    observed = snapshot.observed_at.isoformat()
    if renamed or title_changed or about_changed:
        history.append({
            "username": (locked.unique_name or "") if renamed else None,
            "title": locked.title if title_changed else None,
            "about": (locked.about or "") if about_changed else None,
            "detected_at": observed,
        })
    for prefix, source, expression in updates:
        updated = source.update(link=expression)
        old_name = prefix.rsplit("/", 1)[-1]
        if force and updated and RoomItem.normalize_username(old_name) != RoomItem.normalize_username(snapshot.username):
            if not any(RoomItem.normalize_username(entry.get("username")) == RoomItem.normalize_username(old_name) for entry in history):
                history.append({"username": old_name, "title": None, "about": None, "detected_at": observed})
    if history != locked.username_history:
        changed_fields.add("username_history")
    values = dict(
        unique_name=snapshot.username, link=target, title=snapshot.title,
        username_history=history, username_checked_at=snapshot.observed_at,
        last_update=timezone.now(),
    )
    if snapshot.about_observed:
        values["about"] = snapshot.about or ""
    RoomItem.objects.filter(pk=locked.pk).update(**values)
    locked.unique_name, locked.link, locked.title = snapshot.username, target, snapshot.title
    locked.username_history, locked.username_checked_at = history, snapshot.observed_at
    if snapshot.about_observed:
        locked.about = values["about"]
    if changed_fields:
        mark_room_metadata_pending(locked, changed_fields)
    return locked, {str(locked.pk)} if locked.metadata_sync_pending_at else set()


def reconcile_with_dependencies(room, snapshot, refresh_peer, *, force=False, visited=None):
    """Reconcile a Room after refreshing historical URL owners by peer ID.

    Parameters
    ----------
    room : RoomItem
        Room to reconcile inside the caller's transaction.
    snapshot : RoomIdentitySnapshot
        Its current identity from Telegram.
    refresh_peer : callable
        Return a reliable snapshot using an owner's own credentials and peer ID.
    force : bool, default=False
        Audit historical prefixes even if the stored identity already matches.
    visited : set, optional
        Rooms already visited while resolving username reuse; bounds recursion.

    Returns
    -------
    tuple
        Updated Room and pending IDs, including reconciled historical owners.

    Raises
    ------
    RoomReconciliationError
        Ownership is cyclic, the chain is too deep, or a conflict remains.
    OperationalError
        A required row is locked by another transaction.
    """
    visited = set(visited or ())
    if room.pk in visited or len(visited) >= 10:
        raise RoomReconciliationError("room_username_cycle")
    visited.add(room.pk)
    affected = set()
    try:
        return reconcile_room(room, snapshot, force=force)
    except RoomReconciliationError as exc:
        if not exc.owner_ids:
            raise
        for owner_id in sorted(exc.owner_ids, key=str):
            if owner_id in visited:
                raise RoomReconciliationError("room_username_conflict") from exc
            owner = RoomItem.objects.select_for_update(nowait=True, of=("self",)).get(pk=owner_id)
            _, room_ids = reconcile_with_dependencies(
                owner, refresh_peer(owner), refresh_peer, force=True, visited=visited,
            )
            affected.update(room_ids)
    # Resolve the known owners once. A new conflict defers the checkpoint
    # instead of issuing repeated identity requests in this transaction.
    current, room_ids = reconcile_room(room, snapshot, force=force)
    return current, affected | room_ids


def ensure_message_links_available(messages, refresh_peer=None):
    """Free historical incoming URLs when identity checks are enabled.

    Parameters
    ----------
    messages : sequence of MessageItem
        Incoming messages with URLs based on their current locked Room.
    refresh_peer : callable, optional
        Resolve historical owners by peer ID. Omit when check_room is false.

    Returns
    -------
    set of str
        IDs of historical Rooms needing index synchronization.

    Raises
    ------
    RoomReconciliationError
        Links collide and checks are disabled, or a conflict remains unresolved.

    Notes
    -----
    The caller owns the transaction. Available links require only the initial
    collision check and do not trigger Telegram identity requests.
    """
    owners = conflicting_message_owners(messages)
    if not owners:
        return set()
    if refresh_peer is None:
        raise RoomReconciliationError("message_link_conflict")
    affected = set()
    for owner_id in sorted(owners, key=str):
        owner = RoomItem.objects.select_for_update(nowait=True, of=("self",)).get(pk=owner_id)
        _, room_ids = reconcile_with_dependencies(owner, refresh_peer(owner), refresh_peer, force=True)
        affected.update(room_ids)
    if conflicting_message_owners(messages):
        raise RoomReconciliationError("message_link_conflict")
    return affected

# ======================================================
# =====       DURABLE INDEX SYNCHRONIZATION         =====
# ======================================================


class RoomMetadataWaiting(RuntimeError):
    """An advisory lock or an asynchronous search operation is still active."""


def _task_result(response):
    """Keep the acknowledged Elasticsearch counts and bounded failure details.

    Parameters
    ----------
    response : dict
        Completed Elasticsearch task response, including any top-level error.

    Returns
    -------
    dict
        Updated-document count, timeout/conflict flags and at most three failures.
    """
    result = response.get("response", {})
    return {
        "updated": int(result.get("updated", 0)),
        "timed_out": bool(result.get("timed_out", False)),
        "version_conflicts": int(result.get("version_conflicts", 0)),
        "failures": result.get("failures", [])[:3],
        "error": response.get("error"),
    }


def _record_search_task(batch_id, room_id, request_id, **changes):
    """Persist Elasticsearch task facts only for the matching request.

    Parameters
    ----------
    batch_id : UUID or str
        Batch containing the Room's durable progress.
    room_id : str
        Room progress key.
    request_id : str
        Expected Elasticsearch request ID; outdated results are ignored.
    **changes : dict
        Task ID or result fields to merge into that entry.

    Notes
    -----
    This locks the Batch but requires no stage lease: another index writer may
    observe completion of a task belonging to an earlier worker.
    """
    with transaction.atomic():
        batch = ProcessingBatch.objects.select_for_update(no_key=True, of=("self",)).get(pk=batch_id)
        entry = batch.processor_metadata.get("room_progress", {}).get(room_id, {})
        if entry.get("es_request_id") != request_id:
            return
        entry.update(changes)
        batch.save(update_fields=["processor_metadata", "updated_at"])


def _resolve_search_tasks(room_ids, client=None):
    """Resolve registered Elasticsearch operations before another Room write.

    Parameters
    ----------
    room_ids : iterable of str
        Rooms protected by the caller's advisory locks.
    client : Elasticsearch, optional
        Search client; defaults to the configured connection when needed.

    Raises
    ------
    RoomMetadataWaiting
        At least one operation is still active; the caller must defer its write.

    Notes
    -----
    Pending tasks are polled by ID. A lost submission response is recovered by
    X-Opaque-Id; absent tasks can be reapplied safely. Unchanged Rooms require no
    Batch-history lookup. External requests run without Room row locks.
    """
    # Ordinary index writes need no Batch-history lookup for unchanged Rooms.
    room_ids = [str(pk) for pk in RoomItem.objects.filter(
        pk__in=room_ids, metadata_sync_pending_at__isnull=False,
    ).values_list("pk", flat=True)]
    if not room_ids:
        return
    pending = False
    batches = ProcessingBatch.objects.filter(
        job__processor=TELEGRAM_ROOM_METADATA_PROCESSOR,
        processor_metadata__room_progress__has_any_keys=room_ids,
    ).values_list("pk", "processor_metadata")
    for batch_id, metadata in batches:
        for room_id in room_ids:
            entry = metadata["room_progress"].get(room_id, {})
            request_id = entry.get("es_request_id")
            if not request_id or entry.get("es_result") is not None:
                continue
            client = client if client is not None else connections.get_connection()
            task_id = entry.get("es_task_id")
            if not task_id:
                tasks = client.tasks.list(
                    actions="indices:data/write/update/byquery", detailed=True, group_by="none",
                ).get("tasks", [])
                task_ids = [
                    f"{task['node']}:{task['id']}" for task in tasks
                    if task.get("headers", {}).get("X-Opaque-Id") == request_id
                ]
                if task_ids:
                    task_id = task_ids[0]
                    _record_search_task(batch_id, room_id, request_id, es_task_id=task_id)
                else:
                    # No active task remains. Reapplying the filtered update is safe.
                    _record_search_task(batch_id, room_id, request_id, es_request_id=None, es_task_id=None)
                    continue
            try:
                result = client.tasks.get(task_id=task_id, wait_for_completion=False)
            except Exception as exc:
                if getattr(exc, "status_code", None) != 404:
                    raise
                _record_search_task(batch_id, room_id, request_id, es_request_id=None, es_task_id=None)
                continue
            if result.get("completed"):
                _record_search_task(
                    batch_id, room_id, request_id, es_task_id=None, es_result=_task_result(result),
                )
            else:
                pending = True
    if pending:
        raise RoomMetadataWaiting("Elasticsearch room metadata tasks are still running")


@contextmanager
def room_metadata_write_lock(room_ids):
    """Serialize search/vector writers for each Room across worker processes.

    Parameters
    ----------
    room_ids : iterable
        Room primary keys. Locks are acquired in a stable order.

    Yields
    ------
    None
        All advisory locks are held and earlier Elasticsearch tasks are resolved.

    Raises
    ------
    RoomMetadataWaiting
        Another writer owns a lock or an earlier search operation is still active.

    Notes
    -----
    The two parameterized cursor.execute calls invoke PostgreSQL session advisory
    locks, which have no dedicated QuerySet API. They coordinate writers during
    HTTP requests without retaining a Room row lock or a long SQL transaction.
    Each acquisition is released in finally; closing the database session also
    releases it. All participating index adapters must use this same lock.
    """
    ids = sorted(set(str(value) for value in room_ids if value is not None))
    connection = db_connections["default"]
    acquired = []
    try:
        for room_id in ids:
            key = int.from_bytes(
                hashlib.sha256(f"atenea:room-metadata:{room_id}".encode()).digest()[:8],
                "big", signed=True,
            )
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_try_advisory_lock(%s)", [key])
                locked = cursor.fetchone()[0]
            if not locked:
                raise RoomMetadataWaiting(f"Room {room_id} has another index writer")
            acquired.append(key)
        if ids:
            _resolve_search_tasks(ids)
        yield
    finally:
        for key in reversed(acquired):
            with connection.cursor() as cursor:
                cursor.execute("SELECT pg_advisory_unlock(%s)", [key])


def sync_room_vector_metadata(room_id, username):
    """Update existing Qdrant payloads by stable Room ID, preserving vectors.

    Parameters
    ----------
    room_id : UUID or str
        Room primary key stored in each message point's keyword payload.
    username : str
        Current username to assign to room_unique_name in both configured slots.

    Raises
    ------
    RuntimeError
        Qdrant has not confirmed that the payload update was applied.

    Notes
    -----
    No messages or point IDs are enumerated. The caller holds the Room advisory
    lock; this function closes its Qdrant client after the filtered updates.
    """
    from app_metadata.vector_store import _import_qdrant, get_qdrant_client

    _, models = _import_qdrant()
    selector = models.Filter(must=[
        models.FieldCondition(key="room_id", match=models.MatchValue(value=str(room_id))),
    ])
    client = get_qdrant_client()
    try:
        for collection in sorted(set(settings.QDRANT.collections.values())):
            result = client.set_payload(
                collection_name=collection, points=selector,
                payload={"room_unique_name": username}, wait=True, ordering="strong",
            )
            if getattr(result.status, "value", result.status) != "completed":
                raise RuntimeError(f"Qdrant has not applied the room metadata in {collection!r}")
    finally:
        client.close()


def _save_entry_locked(batch, room_id, changes):
    """Save Room progress and rebuild counters while the Batch is locked.

    Parameters
    ----------
    batch : ProcessingBatch
        Batch already locked in the caller's transaction with a valid stage lease.
    room_id : str
        Room progress key.
    changes : dict
        Progress fields to merge before saving metadata and the stage summary.
    """
    progress = batch.processor_metadata.setdefault("room_progress", {})
    progress.setdefault(room_id, {}).update(changes)
    summary = ProcessingSummary.model_validate(batch.result_summary or {})
    summary.stages[TELEGRAM_ROOM_METADATA_STAGE] = room_metadata_stage_summary(batch)
    batch.processed_count = summary.stages[TELEGRAM_ROOM_METADATA_STAGE].metadata["synchronized_rooms"]
    batch.skipped_count = summary.stages[TELEGRAM_ROOM_METADATA_STAGE].metadata["skipped_rooms"]
    batch.result_summary = summary.model_dump(mode="json")
    batch.save(update_fields=["processor_metadata", "result_summary", "processed_count", "skipped_count", "updated_at"])


def _save_entry(batch_id, token, room_id, **changes):
    """Validate the active stage lease and save a Room progress checkpoint.

    Parameters
    ----------
    batch_id : UUID or str
        Batch to lock inside a short transaction.
    token : str
        Lease token owned by the current worker.
    room_id : str
        Room progress key.
    **changes : dict
        Fields passed to _save_entry_locked.

    Raises
    ------
    StaleStageError
        The worker no longer owns this stage; its changes must not be persisted.
    """
    with transaction.atomic():
        batch = ProcessingBatch.objects.select_for_update(no_key=True, of=("self",)).select_related("job").get(pk=batch_id)
        ensure_stage_lease_locked(batch, stage=TELEGRAM_ROOM_METADATA_STAGE, token=token)
        _save_entry_locked(batch, room_id, changes)


def _sync_room(batch_id, token, room_id):
    """Synchronize one pending Room and conditionally acknowledge its timestamp.

    Parameters
    ----------
    batch_id : UUID or str
        Batch storing the Room's search task and synchronization outcome.
    token : str
        Current stage lease token.
    room_id : str
        Room protected by the caller's advisory lock.

    Raises
    ------
    RoomMetadataWaiting
        Elasticsearch is still processing its registered asynchronous update.
    RuntimeError
        An index write failed, timed out or reported conflicts.

    Notes
    -----
    Index the Room, update search messages for pending name/about/tags changes,
    and patch Qdrant only for name changes. Legacy pending marks without fields
    synchronize all stores once. Clear fields and timestamp together only if
    the captured timestamp still matches; concurrent changes stay pending.
    HTTP requests are outside transactions holding Room row locks.
    """
    from app_telegram.documents import MessageDocument
    from app_telegram.services.index import index_room_ids

    with transaction.atomic():
        batch = ProcessingBatch.objects.select_for_update(no_key=True, of=("self",)).select_related("job").get(pk=batch_id)
        ensure_stage_lease_locked(batch, stage=TELEGRAM_ROOM_METADATA_STAGE, token=token)
        entry = (batch.processor_metadata or {}).get("room_progress", {}).get(room_id, {})
        room = RoomItem.objects.filter(pk=room_id).first()
        if room is None:
            _save_entry_locked(batch, room_id, {"state": "skipped", "error": ""})
            return
        if not room.metadata_sync_pending_at and batch.job.filters.get("room_sync_version") != ROOM_METADATA_SYNC_VERSION:
            room = RoomItem.objects.select_for_update(nowait=True, of=("self",)).get(pk=room_id)
            if room.metadata_sync_pending_at is None:
                mark_room_metadata_pending(room)
        if room.metadata_sync_pending_at is None:
            _save_entry_locked(batch, room_id, {"state": "completed", "error": "", "reason": "already_synchronized"})
            return
        stamp = room.metadata_sync_pending_at
        fields = set(room.metadata_sync_pending_fields or MESSAGE_METADATA_FIELDS)
        target = stamp.isoformat()
        if entry.get("target") != target:
            _save_entry_locked(batch, room_id, {
                "target": target, "state": "pending", "error": "",
                "es_task_id": None, "es_request_id": None, "es_result": None,
                "search_messages_updated": 0,
            })
            entry = batch.processor_metadata["room_progress"][room_id]

    result = entry.get("es_result")
    if result is None:
        index_room_ids([room_id])
    if not fields.intersection(MESSAGE_METADATA_FIELDS):
        result = {"updated": 0}
    elif result is None:
        client = connections.get_connection()
        client.indices.refresh(index=MessageDocument.Index.name)
        request_id = f"atenea-room-metadata:{batch_id}:{room_id}:{uuid.uuid4().hex}"
        _save_entry(batch_id, token, room_id, es_request_id=request_id)
        response = client.options(opaque_id=request_id, max_retries=0).update_by_query(
            index=MessageDocument.Index.name,
            query={"term": {"room_id": room_id}},
            script={
                "lang": "painless",
                "source": (
                    "if (ctx._source.room_name == params.name && "
                    "ctx._source.room_about == params.about && ctx._source.tags == params.tags) { "
                    "ctx.op = 'noop'; } else { "
                    "ctx._source.room_name = params.name; ctx._source.room_about = params.about; "
                    "ctx._source.tags = params.tags; }"
                ),
                "params": {"name": room.unique_name, "about": room.about, "tags": room.tags or []},
            },
            conflicts="proceed", refresh=True, wait_for_completion=False,
        )
        task_id = response.get("task")
        if not isinstance(task_id, str) or not task_id:
            raise RuntimeError("Elasticsearch did not return a metadata task ID")
        _save_entry(batch_id, token, room_id, es_task_id=task_id)
        _resolve_search_tasks([room_id], client)
        entry = ProcessingBatch.objects.get(pk=batch_id).processor_metadata["room_progress"][room_id]
        result = entry.get("es_result")
        if result is None:
            raise RoomMetadataWaiting("Elasticsearch metadata task needs recovery")
    if result.get("error") or result.get("failures") or result.get("timed_out") or result.get("version_conflicts"):
        _save_entry(batch_id, token, room_id, es_request_id=None, es_task_id=None, es_result=None)
        raise RuntimeError(f"Elasticsearch room metadata update was incomplete: {result}")

    if "unique_name" in fields:
        sync_room_vector_metadata(room_id, room.unique_name)
    with transaction.atomic():
        batch = ProcessingBatch.objects.select_for_update(no_key=True, of=("self",)).select_related("job").get(pk=batch_id)
        ensure_stage_lease_locked(batch, stage=TELEGRAM_ROOM_METADATA_STAGE, token=token)
        # Scan holds Room before enqueueing into this Job. Never wait for that
        # Room while holding the Batch/Job locks in the opposite order.
        RoomItem.objects.select_for_update(nowait=True, of=("self",)).filter(pk=room_id).first()
        cleared = RoomItem.objects.filter(pk=room_id, metadata_sync_pending_at=stamp).update(
            metadata_sync_pending_at=None, metadata_sync_pending_fields=[],
        )
        _save_entry_locked(batch, room_id, {
            "state": "completed" if cleared else "waiting", "error": "",
            "reason": "" if cleared else "room_changed_during_sync",
            "search_messages_updated": result["updated"],
        })


def project_room_metadata(batch_id, token):
    """Process pending Rooms independently and preserve confirmed successes.

    Parameters
    ----------
    batch_id : UUID or str
        Batch with Room IDs to synchronize.
    token : str
        Current stage lease token.

    Returns
    -------
    StageSummary
        Durable successes, pending Rooms and individual errors across attempts.

    Raises
    ------
    StaleStageError
        The worker lost its lease and must stop.
    SoftTimeLimitExceeded
        Celery must retry the remaining work.

    Notes
    -----
    Lock contention and active search tasks defer a Room. Other failures are
    recorded for retry while processing continues with the remaining Rooms.
    """
    batch = ProcessingBatch.objects.get(pk=batch_id)
    for room_id in map(str, batch.item_ids):
        if (batch.processor_metadata or {}).get("room_progress", {}).get(room_id, {}).get("state") in {"completed", "skipped"}:
            continue
        try:
            with room_metadata_write_lock([room_id]):
                _sync_room(batch_id, token, room_id)
        except (StaleStageError, SoftTimeLimitExceeded):
            raise
        except Exception as exc:
            waiting = isinstance(exc, RoomMetadataWaiting) or (
                isinstance(exc, OperationalError) and getattr(exc.__cause__, "pgcode", None) == "55P03"
            )
            _save_entry(batch_id, token, room_id, state="waiting" if waiting else "failed", error=str(exc))
    return room_metadata_stage_summary(ProcessingBatch.objects.get(pk=batch_id))
