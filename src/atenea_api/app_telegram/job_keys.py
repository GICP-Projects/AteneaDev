"""Stable processor and stage identifiers owned by the Telegram app.

Kept dependency-free so the task registry in Django settings can import the
same identifiers before the app registry is ready.
"""

TELEGRAM_SCAN_PROCESSOR = "telegram.scan.rooms"
TELEGRAM_SCAN_COMMENTS_PROCESSOR = "telegram.scan.comments"
TELEGRAM_PROCESS_MESSAGES_PROCESSOR = "telegram.process_messages"
TELEGRAM_INDEX_MESSAGES_PROCESSOR = "telegram.index.messages"
TELEGRAM_MESSAGE_ENTITY_SYNC_PROCESSOR = "telegram.message_entity.sync"
TELEGRAM_ROOM_METADATA_PROCESSOR = "telegram.room_metadata.sync"
TELEGRAM_POPULATE_PROCESSOR = "telegram.populate"
TELEGRAM_ACCESS_PROCESSOR = "telegram.access.rooms"

TELEGRAM_SCAN_STAGE = "telegram.scan.rooms.collect_messages"
TELEGRAM_SCAN_COMMENTS_STAGE = "telegram.scan.comments.collect_comments"
TELEGRAM_ACCESS_STAGE = "telegram.access.rooms.resolve_hashes"
TELEGRAM_INDEX_STAGE = "telegram.index.messages.write"
TELEGRAM_MESSAGE_ENTITY_SYNC_STAGE = "telegram.message_entity.sync.write"
TELEGRAM_ROOM_METADATA_STAGE = "telegram.room_metadata.sync.write"
TELEGRAM_POPULATE_STAGE = "telegram.populate.seeds.resolve"
TELEGRAM_INDEX_ROOMS_STAGE = "telegram.index.rooms_from_seeds"

TELEGRAM_POPULATE_WORKFLOW = [TELEGRAM_POPULATE_STAGE, TELEGRAM_INDEX_ROOMS_STAGE]
