from types import SimpleNamespace
from unittest.mock import patch
from uuid import uuid4

from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from django.test import TestCase, override_settings
from django.utils import timezone

from app_base.views import parse_request_data
from app_telegram.models import MessageItem, RoomItem, TelegramAuth, TelegramExternalUrlItem, TelegramMediaItem
from app_telegram.serializers import (
    DownloadableCatalogSerializer,
    DownloadableDeleteSerializer,
    MediaDownloadSerializer,
    MediaDownloadStatusSerializer,
)
from app_telegram.services import media_progress
from app_telegram.services.media import (
    DOWNLOADABLE_MEDIA_TYPES,
    build_downloadable_catalog,
    collect_external_urls,
    delete_downloadable_items,
    download_telegram_media,
    media_download_pipeline,
    schedule_telegram_media_download,
)
from app_telegram.services.media_storage import (
    classify_media_risk,
    is_whitelisted_download_domain,
    provider_from_domain,
)


class TelegramMediaPolicyTests(TestCase):
    def test_downloadable_media_types_are_real_telegram_media(self):
        self.assertEqual(
            DOWNLOADABLE_MEDIA_TYPES,
            [
                MessageItem.PHOTO,
                MessageItem.VIDEO,
                MessageItem.AUDIO,
                MessageItem.DOC,
                MessageItem.GIF,
            ],
        )
        self.assertNotIn(MessageItem.TEXT, DOWNLOADABLE_MEDIA_TYPES)
        self.assertNotIn(MessageItem.WEB_PAGE, DOWNLOADABLE_MEDIA_TYPES)

    def test_executable_extensions_are_downloaded_but_marked_high_risk(self):
        risk = classify_media_risk(
            ".exe",
            "application/vnd.microsoft.portable-executable",
        )

        self.assertEqual(risk.level, TelegramMediaItem.RISK_HIGH)
        self.assertEqual(risk.reason, "executable_extension")
        self.assertTrue(risk.potentially_dangerous)

    def test_regular_document_is_low_risk(self):
        risk = classify_media_risk(".pdf", "application/pdf")

        self.assertEqual(risk.level, TelegramMediaItem.RISK_LOW)
        self.assertEqual(risk.reason, "")
        self.assertFalse(risk.potentially_dangerous)


class ExternalUrlPolicyTests(TestCase):
    def test_default_cloud_domains_are_whitelisted(self):
        self.assertTrue(is_whitelisted_download_domain("drive.google.com"))
        self.assertTrue(is_whitelisted_download_domain("dl.dropboxusercontent.com"))
        self.assertTrue(is_whitelisted_download_domain("mega.nz"))

    @override_settings(MEDIA_EXTERNAL_URL_WHITELIST=["nextcloud.*", "owncloud.*"])
    def test_configured_cloud_patterns_are_whitelisted(self):
        self.assertTrue(is_whitelisted_download_domain("files.nextcloud.example.org"))
        self.assertTrue(is_whitelisted_download_domain("download.owncloud.internal"))

    def test_provider_detection_groups_known_domains(self):
        self.assertEqual(provider_from_domain("drive.google.com"), "google")
        self.assertEqual(provider_from_domain("1drv.ms"), "onedrive")
        self.assertEqual(provider_from_domain("mega.nz"), "mega")

    def test_collect_external_urls_uses_message_room_id(self):
        room = RoomItem.objects.create(
            tg_id=12345,
            unique_name="canal_demo",
            link="https://t.me/canal_demo",
            title="Canal Demo",
            is_channel=True,
            created_at=timezone.now(),
        )
        message = MessageItem.objects.create(
            link="https://t.me/canal_demo/1",
            msg_id=1,
            room=room,
            media_type=MessageItem.TEXT,
            text="Archivo: https://drive.google.com/file/d/demo",
            created_at=timezone.now(),
        )

        created = collect_external_urls([str(message.pk)], use_text_fallback=True)

        self.assertEqual(created, 1)
        external_url = TelegramExternalUrlItem.objects.get(message=message)
        self.assertEqual(external_url.room_id, room.pk)
        self.assertEqual(external_url.provider, "google")


class DownloadableCatalogSerializerTests(TestCase):
    def test_accepts_expected_sources_and_statuses(self):
        serializer = DownloadableCatalogSerializer(
            data={
                "source": "bucket",
                "status": "downloaded",
                "ext": ["pdf"],
                "limit": 10,
                "max_items": 5,
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_rejects_unknown_status(self):
        serializer = DownloadableCatalogSerializer(data={"status": "executed"})

        self.assertFalse(serializer.is_valid())
        self.assertIn("status", serializer.errors)

    def test_accepts_deleted_status(self):
        serializer = DownloadableCatalogSerializer(data={"status": "deleted"})

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_downloadable_catalog_ext_filter_returns_only_bucket_media(self):
        room = RoomItem.objects.create(
            tg_id=98770,
            unique_name="canal_catalog_ext",
            link="https://t.me/canal_catalog_ext",
            title="Canal Catalog Ext",
            is_channel=True,
            created_at=timezone.now(),
        )
        exe_message = MessageItem.objects.create(
            link="https://t.me/canal_catalog_ext/1",
            msg_id=1,
            room=room,
            media_type=MessageItem.DOC,
            created_at=timezone.now(),
        )
        pdf_message = MessageItem.objects.create(
            link="https://t.me/canal_catalog_ext/2",
            msg_id=2,
            room=room,
            media_type=MessageItem.DOC,
            created_at=timezone.now(),
        )
        url_message = MessageItem.objects.create(
            link="https://t.me/canal_catalog_ext/3",
            msg_id=3,
            room=room,
            media_type=MessageItem.TEXT,
            created_at=timezone.now(),
        )
        TelegramMediaItem.objects.create(
            message=exe_message,
            room=room,
            status=TelegramMediaItem.SKIPPED,
            original_file_name="tool.exe",
            extension=".exe",
        )
        TelegramMediaItem.objects.create(
            message=pdf_message,
            room=room,
            status=TelegramMediaItem.SKIPPED,
            original_file_name="doc.pdf",
            extension=".pdf",
        )
        TelegramExternalUrlItem.objects.create(
            message=url_message,
            room=room,
            url="https://drive.google.com/file/d/demo",
            domain="drive.google.com",
            provider="google",
        )

        results = build_downloadable_catalog(room=["canal_catalog_ext"], ext=["exe"])

        self.assertEqual(len(results), 1)
        self.assertEqual(len(results[0]["items"]), 1)
        self.assertEqual(results[0]["items"][0]["source"], "bucket")
        self.assertEqual(results[0]["items"][0]["extension"], ".exe")


class MediaDownloadSerializerTests(TestCase):
    def test_metadata_only_defaults_to_false(self):
        serializer = MediaDownloadSerializer(data={})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertFalse(serializer.validated_data["metadata_only"])

    def test_metadata_only_accepts_query_boolean(self):
        request = Request(
            APIRequestFactory().get(
                "/api/v1/tg/msg/media/download?metadata_only=true&room=canal_media"
            )
        )

        params, _ = parse_request_data(
            request,
            MediaDownloadSerializer,
            to_log_query=False,
        )

        self.assertTrue(params["metadata_only"])
        self.assertEqual(params["room"], ["canal_media"])


class MediaDownloadTaskTests(TestCase):
    def test_media_download_pipeline_queues_scheduler_without_counting_messages(self):
        token = uuid4()

        with (
            patch("app_telegram.services.media.init_progress") as init_progress,
            patch("app_telegram.services.media.schedule_telegram_media_download.apply_async") as apply_async,
        ):
            result = media_download_pipeline(
                token=token,
                room=["canal_media"],
                metadata_only=True,
            )

        self.assertIsNone(result)
        init_progress.assert_called_once_with(token, total=0, total_chunks=0)
        apply_async.assert_called_once()
        self.assertEqual(
            apply_async.call_args.kwargs["kwargs"]["token"],
            str(token),
        )
        self.assertEqual(
            apply_async.call_args.kwargs["kwargs"]["room"],
            ["canal_media"],
        )
        self.assertTrue(apply_async.call_args.kwargs["kwargs"]["metadata_only"])

    def test_scheduler_enqueues_chunks_and_updates_progress(self):
        fake_redis = FakeRedis()
        token = uuid4()
        auth = TelegramAuth.objects.create(
            name="fake",
            api_id=111,
            api_hash="hash",
            session="session",
            is_valid=True,
        )
        room = RoomItem.objects.create(
            tg_id=67891,
            access_hash=98766,
            access_auth=auth,
            unique_name="canal_scheduler",
            link="https://t.me/canal_scheduler",
            title="Canal Scheduler",
            is_channel=True,
            created_at=timezone.now(),
        )
        message = MessageItem.objects.create(
            link="https://t.me/canal_scheduler/1",
            msg_id=1,
            room=room,
            media_type=MessageItem.DOC,
            created_at=timezone.now(),
        )

        with (
            patch("app_telegram.services.media_progress._client", return_value=fake_redis),
            patch("app_telegram.services.media.download_telegram_media.apply_async") as apply_async,
        ):
            media_progress.init_progress(token, total=0, total_chunks=0)
            total = schedule_telegram_media_download(
                token=token.hex,
                room=["canal_scheduler"],
                metadata_only=True,
            )
            progress = media_progress.get_progress(token)

        self.assertEqual(total, 1)
        apply_async.assert_called_once()
        self.assertEqual(apply_async.call_args.kwargs["args"][0], [str(message.pk)])
        self.assertTrue(apply_async.call_args.kwargs["kwargs"]["metadata_only"])
        self.assertEqual(progress["status"], "running")
        self.assertEqual(progress["total"], 1)
        self.assertEqual(progress["chunks_scheduled"], 1)
        self.assertEqual(progress["messages_scheduled"], 1)
        self.assertEqual(progress["total_chunks"], 1)
        self.assertTrue(progress["schedule_completed"])

    def test_metadata_only_stores_metadata_without_downloading_binary(self):
        room = RoomItem.objects.create(
            tg_id=67890,
            access_hash=98765,
            unique_name="canal_metadata",
            link="https://t.me/canal_metadata",
            title="Canal Metadata",
            is_channel=True,
            created_at=timezone.now(),
        )
        message = MessageItem.objects.create(
            link="https://t.me/canal_metadata/1",
            msg_id=1,
            room=room,
            media_type=MessageItem.DOC,
            created_at=timezone.now(),
        )

        class FakeTelegramMessage:
            media = object()
            photo = None
            file = SimpleNamespace(
                name="tool.exe",
                mime_type="application/vnd.microsoft.portable-executable",
                size=999_999_999,
                ext=".exe",
            )

            async def download_media(self, file=None):
                raise AssertionError("metadata_only must not download media bytes")

        class FakeTelegramClient:
            def __init__(self, *args, **kwargs):
                pass

            async def __aenter__(self):
                return self

            async def __aexit__(self, exc_type, exc, tb):
                pass

            async def start(self):
                pass

            async def get_messages(self, input_room, ids):
                return FakeTelegramMessage()

        with patch("app_telegram.services.media.TelegramClient", FakeTelegramClient):
            download_telegram_media(
                [str(message.pk)],
                {"pk": str(uuid4()), "name": "fake", "api_id": 1, "api_hash": "hash", "session": ""},
                metadata_only=True,
            )

        media = TelegramMediaItem.objects.get(message=message)
        self.assertEqual(media.status, TelegramMediaItem.SKIPPED)
        self.assertEqual(media.reason, "metadata_only")
        self.assertEqual(media.original_file_name, "tool.exe")
        self.assertEqual(media.extension, ".exe")
        self.assertEqual(media.mime_type, "application/vnd.microsoft.portable-executable")
        self.assertEqual(media.size_bytes, 999_999_999)
        self.assertEqual(media.object_key, "")
        self.assertEqual(media.sha256, "")
        self.assertIsNone(media.downloaded_at)
        self.assertEqual(media.risk_level, TelegramMediaItem.RISK_HIGH)
        self.assertTrue(media.is_potentially_dangerous)


class DownloadableDeleteTests(TestCase):
    def test_delete_query_params_are_parsed(self):
        request = Request(
            APIRequestFactory().delete(
                "/api/v1/tg/msg/downloadable?room=canal_delete&ext=mp4&ext=exe&dry_run=false&confirm=true"
            )
        )

        params, _ = parse_request_data(
            request,
            DownloadableDeleteSerializer,
            to_log_query=False,
        )

        self.assertEqual(params["room"], ["canal_delete"])
        self.assertEqual(params["ext"], ["mp4", "exe"])
        self.assertFalse(params["dry_run"])
        self.assertTrue(params["confirm"])

    def test_delete_requires_room_or_tag_filter(self):
        serializer = DownloadableDeleteSerializer(data={"ext": ["exe"]})

        self.assertFalse(serializer.is_valid())
        self.assertIn("non_field_errors", serializer.errors)

    def test_delete_serializer_accepts_file_filters(self):
        serializer = DownloadableDeleteSerializer(
            data={
                "room": ["canal_delete"],
                "ext": ["mp4", "exe"],
                "min_size_bytes": 1024,
                "dry_run": True,
                "confirm": False,
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)

    def test_delete_serializer_ignores_removed_filters(self):
        serializer = DownloadableDeleteSerializer(
            data={
                "room": ["canal_delete"],
                "status": "downloaded",
                "lang": ["es"],
                "is_reply": False,
                "stored_since": "2026-05-01",
            }
        )

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertNotIn("status", serializer.validated_data)
        self.assertNotIn("lang", serializer.validated_data)
        self.assertNotIn("is_reply", serializer.validated_data)
        self.assertNotIn("stored_since", serializer.validated_data)
        self.assertFalse(serializer.validated_data["dry_run"])

    def test_delete_bucket_media_removes_object_and_marks_deleted(self):
        room = RoomItem.objects.create(
            tg_id=98765,
            unique_name="canal_delete",
            link="https://t.me/canal_delete",
            title="Canal Delete",
            is_channel=True,
            created_at=timezone.now(),
        )
        message = MessageItem.objects.create(
            link="https://t.me/canal_delete/1",
            msg_id=1,
            room=room,
            media_type=MessageItem.DOC,
            text="tool",
            created_at=timezone.now(),
        )
        media = TelegramMediaItem.objects.create(
            message=message,
            room=room,
            status=TelegramMediaItem.DOWNLOADED,
            bucket="atenea-telegram-media",
            object_key="telegram-media/canal_delete/1/tool.exe",
            original_file_name="tool.exe",
            extension=".exe",
        )

        with patch("app_telegram.services.media.delete_object") as delete_object_mock:
            result = delete_downloadable_items(
                room=["canal_delete"],
                dry_run=False,
            )

        delete_object_mock.assert_called_once_with(media.object_key)
        media.refresh_from_db()
        self.assertEqual(result["media_marked_deleted"], 1)
        self.assertEqual(result["s3_objects_deleted"], 1)
        self.assertEqual(media.status, TelegramMediaItem.DELETED)
        self.assertEqual(media.reason, "deleted_by_request")

    def test_delete_bucket_media_dry_run_does_not_delete_or_mark_rows(self):
        room = RoomItem.objects.create(
            tg_id=98767,
            unique_name="canal_dry_run",
            link="https://t.me/canal_dry_run",
            title="Canal Dry Run",
            is_channel=True,
            created_at=timezone.now(),
        )
        message = MessageItem.objects.create(
            link="https://t.me/canal_dry_run/1",
            msg_id=1,
            room=room,
            media_type=MessageItem.DOC,
            created_at=timezone.now(),
        )
        media = TelegramMediaItem.objects.create(
            message=message,
            room=room,
            status=TelegramMediaItem.DOWNLOADED,
            bucket="atenea-telegram-media",
            object_key="telegram-media/canal_dry_run/1/big.bin",
            original_file_name="big.bin",
            extension=".bin",
            size_bytes=2048,
        )

        with patch("app_telegram.services.media.delete_object") as delete_object_mock:
            result = delete_downloadable_items(room=["canal_dry_run"], dry_run=True)

        delete_object_mock.assert_not_called()
        media.refresh_from_db()
        self.assertTrue(result["dry_run"])
        self.assertEqual(result["media_matched"], 1)
        self.assertEqual(result["s3_objects_matched"], 1)
        self.assertEqual(result["s3_objects_deleted"], 0)
        self.assertEqual(media.status, TelegramMediaItem.DOWNLOADED)

    def test_delete_bucket_media_can_filter_by_min_size_bytes(self):
        room = RoomItem.objects.create(
            tg_id=98768,
            unique_name="canal_size",
            link="https://t.me/canal_size",
            title="Canal Size",
            is_channel=True,
            created_at=timezone.now(),
        )
        small_message = MessageItem.objects.create(
            link="https://t.me/canal_size/1",
            msg_id=1,
            room=room,
            media_type=MessageItem.DOC,
            created_at=timezone.now(),
        )
        big_message = MessageItem.objects.create(
            link="https://t.me/canal_size/2",
            msg_id=2,
            room=room,
            media_type=MessageItem.DOC,
            created_at=timezone.now(),
        )
        TelegramMediaItem.objects.create(
            message=small_message,
            room=room,
            status=TelegramMediaItem.DOWNLOADED,
            object_key="telegram-media/canal_size/1/small.bin",
            size_bytes=100,
        )
        TelegramMediaItem.objects.create(
            message=big_message,
            room=room,
            status=TelegramMediaItem.DOWNLOADED,
            object_key="telegram-media/canal_size/2/big.bin",
            size_bytes=5000,
        )

        result = delete_downloadable_items(
            room=["canal_size"],
            min_size_bytes=1024,
            dry_run=True,
        )

        self.assertEqual(result["media_matched"], 1)
        self.assertEqual(result["s3_objects_matched"], 1)

    def test_delete_bucket_media_only_matches_downloaded_status(self):
        room = RoomItem.objects.create(
            tg_id=98773,
            unique_name="canal_delete_status",
            link="https://t.me/canal_delete_status",
            title="Canal Delete Status",
            is_channel=True,
            created_at=timezone.now(),
        )
        downloaded_message = MessageItem.objects.create(
            link="https://t.me/canal_delete_status/1",
            msg_id=1,
            room=room,
            media_type=MessageItem.DOC,
            created_at=timezone.now(),
        )
        skipped_message = MessageItem.objects.create(
            link="https://t.me/canal_delete_status/2",
            msg_id=2,
            room=room,
            media_type=MessageItem.DOC,
            created_at=timezone.now(),
        )
        TelegramMediaItem.objects.create(
            message=downloaded_message,
            room=room,
            status=TelegramMediaItem.DOWNLOADED,
            object_key="telegram-media/canal_delete_status/1/file.bin",
        )
        TelegramMediaItem.objects.create(
            message=skipped_message,
            room=room,
            status=TelegramMediaItem.SKIPPED,
            object_key="telegram-media/canal_delete_status/2/file.bin",
        )

        result = delete_downloadable_items(room=["canal_delete_status"], dry_run=True)

        self.assertEqual(result["media_matched"], 1)
        self.assertEqual(result["s3_objects_matched"], 1)

    def test_delete_bucket_media_can_filter_by_multiple_extensions(self):
        room = RoomItem.objects.create(
            tg_id=98769,
            unique_name="canal_extensions",
            link="https://t.me/canal_extensions",
            title="Canal Extensions",
            is_channel=True,
            created_at=timezone.now(),
        )
        exe_message = MessageItem.objects.create(
            link="https://t.me/canal_extensions/1",
            msg_id=1,
            room=room,
            media_type=MessageItem.DOC,
            created_at=timezone.now(),
        )
        pdf_message = MessageItem.objects.create(
            link="https://t.me/canal_extensions/2",
            msg_id=2,
            room=room,
            media_type=MessageItem.DOC,
            created_at=timezone.now(),
        )
        TelegramMediaItem.objects.create(
            message=exe_message,
            room=room,
            status=TelegramMediaItem.DOWNLOADED,
            object_key="telegram-media/canal_extensions/1/tool.exe",
            extension=".exe",
        )
        TelegramMediaItem.objects.create(
            message=pdf_message,
            room=room,
            status=TelegramMediaItem.DOWNLOADED,
            object_key="telegram-media/canal_extensions/2/doc.pdf",
            extension=".pdf",
        )

        result = delete_downloadable_items(
            room=["canal_extensions"],
            ext=["exe", "mp4"],
            dry_run=True,
        )

        self.assertEqual(result["media_matched"], 1)
        self.assertEqual(result["s3_objects_matched"], 1)

    @override_settings(MEDIA_DOWNLOADABLE_DELETE_CONFIRM_THRESHOLD=1)
    def test_delete_bucket_media_dry_run_reports_confirmation_requirement(self):
        room = RoomItem.objects.create(
            tg_id=98774,
            unique_name="canal_confirm_dry_run",
            link="https://t.me/canal_confirm_dry_run",
            title="Canal Confirm Dry Run",
            is_channel=True,
            created_at=timezone.now(),
        )
        for msg_id in range(1, 3):
            message = MessageItem.objects.create(
                link=f"https://t.me/canal_confirm_dry_run/{msg_id}",
                msg_id=msg_id,
                room=room,
                media_type=MessageItem.DOC,
                created_at=timezone.now(),
            )
            TelegramMediaItem.objects.create(
                message=message,
                room=room,
                status=TelegramMediaItem.DOWNLOADED,
                object_key=f"telegram-media/canal_confirm_dry_run/{msg_id}/file.bin",
            )

        with patch("app_telegram.services.media.delete_object") as delete_object_mock:
            result = delete_downloadable_items(
                room=["canal_confirm_dry_run"],
                dry_run=True,
            )

        delete_object_mock.assert_not_called()
        self.assertTrue(result["dry_run"])
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["media_matched"], 2)
        self.assertEqual(result["media_marked_deleted"], 0)

    @override_settings(MEDIA_DOWNLOADABLE_DELETE_CONFIRM_THRESHOLD=1)
    def test_delete_bucket_media_requires_confirm_above_threshold(self):
        room = RoomItem.objects.create(
            tg_id=98771,
            unique_name="canal_confirm",
            link="https://t.me/canal_confirm",
            title="Canal Confirm",
            is_channel=True,
            created_at=timezone.now(),
        )
        for msg_id in range(1, 3):
            message = MessageItem.objects.create(
                link=f"https://t.me/canal_confirm/{msg_id}",
                msg_id=msg_id,
                room=room,
                media_type=MessageItem.DOC,
                created_at=timezone.now(),
            )
            TelegramMediaItem.objects.create(
                message=message,
                room=room,
                status=TelegramMediaItem.DOWNLOADED,
                object_key=f"telegram-media/canal_confirm/{msg_id}/file.bin",
            )

        with patch("app_telegram.services.media.delete_object") as delete_object_mock:
            result = delete_downloadable_items(
                room=["canal_confirm"],
                dry_run=False,
            )

        delete_object_mock.assert_not_called()
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["media_matched"], 2)
        self.assertEqual(result["media_marked_deleted"], 0)

    @override_settings(MEDIA_DOWNLOADABLE_DELETE_CONFIRM_THRESHOLD=1)
    def test_delete_bucket_media_confirm_allows_large_delete(self):
        room = RoomItem.objects.create(
            tg_id=98772,
            unique_name="canal_confirmed",
            link="https://t.me/canal_confirmed",
            title="Canal Confirmed",
            is_channel=True,
            created_at=timezone.now(),
        )
        for msg_id in range(1, 3):
            message = MessageItem.objects.create(
                link=f"https://t.me/canal_confirmed/{msg_id}",
                msg_id=msg_id,
                room=room,
                media_type=MessageItem.DOC,
                created_at=timezone.now(),
            )
            TelegramMediaItem.objects.create(
                message=message,
                room=room,
                status=TelegramMediaItem.DOWNLOADED,
                object_key=f"telegram-media/canal_confirmed/{msg_id}/file.bin",
            )

        with patch("app_telegram.services.media.delete_object") as delete_object_mock:
            result = delete_downloadable_items(
                room=["canal_confirmed"],
                dry_run=False,
                confirm=True,
            )

        self.assertEqual(delete_object_mock.call_count, 2)
        self.assertTrue(result["requires_confirmation"])
        self.assertEqual(result["media_marked_deleted"], 2)

    def test_downloadable_catalog_hides_deleted_by_default(self):
        room = RoomItem.objects.create(
            tg_id=98766,
            unique_name="canal_hidden",
            link="https://t.me/canal_hidden",
            title="Canal Hidden",
            is_channel=True,
            created_at=timezone.now(),
        )
        message = MessageItem.objects.create(
            link="https://t.me/canal_hidden/1",
            msg_id=1,
            room=room,
            media_type=MessageItem.DOC,
            created_at=timezone.now(),
        )
        TelegramMediaItem.objects.create(
            message=message,
            room=room,
            status=TelegramMediaItem.DELETED,
            original_file_name="deleted.pdf",
            extension=".pdf",
        )

        results = build_downloadable_catalog(room=["canal_hidden"], source="bucket")
        deleted_results = build_downloadable_catalog(
            room=["canal_hidden"],
            source="bucket",
            status=TelegramMediaItem.DELETED,
        )

        self.assertEqual(results, [])
        self.assertEqual(len(deleted_results), 1)


class MediaDownloadProgressTests(TestCase):
    def test_status_serializer_accepts_uuid_token(self):
        token = uuid4()
        serializer = MediaDownloadStatusSerializer(data={"token": str(token)})

        self.assertTrue(serializer.is_valid(), serializer.errors)
        self.assertEqual(serializer.validated_data["token"], token.hex)

    def test_progress_lifecycle_uses_token_counters(self):
        fake_redis = FakeRedis()
        token = uuid4()

        with patch("app_telegram.services.media_progress._client", return_value=fake_redis):
            media_progress.init_progress(token, total=4, total_chunks=2)
            media_progress.mark_schedule_completed(token)
            media_progress.increment_progress(token, processed=2, downloaded=1, skipped=1)
            media_progress.complete_chunk(token)
            progress = media_progress.get_progress(token)

        self.assertEqual(progress["status"], "running")
        self.assertEqual(progress["total"], 4)
        self.assertEqual(progress["processed"], 2)
        self.assertEqual(progress["downloaded"], 1)
        self.assertEqual(progress["skipped"], 1)
        self.assertEqual(progress["chunks_completed"], 1)
        self.assertEqual(progress["percent"], 50.0)
        self.assertTrue(progress["schedule_completed"])

    def test_progress_reports_scheduler_counters(self):
        fake_redis = FakeRedis()
        token = uuid4()

        with patch("app_telegram.services.media_progress._client", return_value=fake_redis):
            media_progress.init_progress(token, total=0, total_chunks=0)
            media_progress.mark_scheduling(token)
            media_progress.set_schedule_total(token, total=10)
            media_progress.increment_scheduled(token, chunks=2, messages=4)
            progress = media_progress.get_progress(token)

        self.assertEqual(progress["status"], "scheduling")
        self.assertEqual(progress["total"], 10)
        self.assertEqual(progress["total_chunks"], 2)
        self.assertEqual(progress["chunks_scheduled"], 2)
        self.assertEqual(progress["messages_scheduled"], 4)
        self.assertEqual(progress["scheduling_percent"], 40.0)
        self.assertFalse(progress["schedule_completed"])


class FakeRedis:
    def __init__(self):
        self.data = {}

    def hset(self, key, field=None, value=None, mapping=None):
        self.data.setdefault(key, {})
        if mapping is not None:
            self.data[key].update({k: str(v) for k, v in mapping.items()})
        elif field is not None:
            self.data[key][field] = str(value)

    def hincrby(self, key, field, amount):
        self.data.setdefault(key, {})
        value = int(self.data[key].get(field, 0)) + amount
        self.data[key][field] = str(value)
        return value

    def hgetall(self, key):
        return dict(self.data.get(key, {}))

    def expire(self, key, seconds):
        return True

    def pipeline(self):
        return self

    def execute(self):
        return []
