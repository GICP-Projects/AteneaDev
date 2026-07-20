from unittest.mock import Mock, patch

import pytest

from app_telegram.models import MessageItem, RoomItem
from app_telegram.services.pipelines import (
    load_balancer_group_pks_by_auth,
    scan_comments_pipeline,
    scan_pipeline,
)
from app_telegram.services.telegram import scan_rooms


@pytest.mark.django_db
def test_scan_pipeline_selects_only_scannable_rooms(
    room_factory,
    telegram_auth_factory,
    query,
):
    auth = telegram_auth_factory()
    eligible = room_factory(
        tg_id=20001,
        unique_name="eligible",
        link="https://t.me/eligible",
        access_auth=auth,
        access_hash=101,
        tags=["paper"],
        lang="en",
    )
    room_factory(
        tg_id=20002,
        unique_name="private",
        link="https://t.me/private",
        access_auth=auth,
        is_private=True,
    )
    room_factory(
        tg_id=20003,
        unique_name="deleted",
        link="https://t.me/deleted",
        access_auth=auth,
        is_deleted=True,
    )
    room_factory(
        tg_id=20004,
        unique_name="not_followed",
        link="https://t.me/not_followed",
        access_auth=auth,
        following=False,
    )

    with patch(
        "app_telegram.services.pipelines.run_generic_pipeline",
        return_value=1,
    ) as run_pipeline:
        count = scan_pipeline(
            tags=["paper"],
            lang=["en"],
            max_msgs=321,
            update_users=True,
            token=query.pk,
        )

    assert count == 1
    kwargs = run_pipeline.call_args.kwargs
    assert list(kwargs["queryset"].values_list("pk", flat=True)) == [eligible.pk]
    assert kwargs["main_task_func"] is scan_rooms
    assert kwargs["extra_args"] == {
        "token": query.pk,
        "max_msgs": 321,
        "update_existing_users": True,
    }
    signatures = kwargs["tasks_signatures_to_chain"]
    assert [signature.task for signature in signatures] == [
        "app_telegram.services.telegram.postprocess_msgs",
        "app_entity.services.ner.ner_db_store",
    ]
    assert kwargs["callback_task_func"].name == "app_telegram.services.telegram.items_to_index"


@pytest.mark.django_db
def test_scan_comments_pipeline_builds_message_filters(
    room_factory,
    telegram_auth_factory,
    query,
):
    auth = telegram_auth_factory()
    channel = room_factory(
        tg_id=21001,
        unique_name="comments_channel",
        link="https://t.me/comments_channel",
        access_auth=auth,
        access_hash=202,
        is_channel=True,
    )

    with patch(
        "app_telegram.services.pipelines.run_generic_pipeline",
        return_value=1,
    ) as run_pipeline:
        count = scan_comments_pipeline(
            room=["comments_channel"],
            langs=["es"],
            types=[MessageItem.TEXT],
            max_msgs=50,
            total_msgs=10000,
            token=query.pk,
        )

    assert count == 1
    kwargs = run_pipeline.call_args.kwargs
    assert list(kwargs["queryset"].values_list("pk", flat=True)) == [channel.pk]
    assert kwargs["extra_args"]["message_filters"] == {
        "and_filter_fields": {
            "lang__in": ["es"],
            "media_type__in": [MessageItem.TEXT],
            "created_at__gte": None,
            "created_at__lte": None,
            "stored_date__gte": None,
            "is_valid": True,
            "is_reply": False,
        }
    }
    assert kwargs["extra_args"]["max_msgs"] == 50
    assert kwargs["extra_args"]["total_msgs"] == 10000


@pytest.mark.django_db
def test_group_load_balancer_assigns_rooms_to_their_access_credentials(
    room_factory,
    telegram_auth_factory,
):
    first_auth = telegram_auth_factory(name="first-auth", api_id=50001, api_hash="first")
    second_auth = telegram_auth_factory(name="second-auth", api_id=50002, api_hash="second")
    first_room = room_factory(
        tg_id=22001,
        unique_name="first_room",
        link="https://t.me/first_room",
        access_auth=first_auth,
    )
    second_room = room_factory(
        tg_id=22002,
        unique_name="second_room",
        link="https://t.me/second_room",
        access_auth=second_auth,
    )
    room_factory(
        tg_id=22003,
        unique_name="orphan_room",
        link="https://t.me/orphan_room",
        access_auth=None,
    )
    task = Mock()
    task.s.side_effect = lambda *args, **kwargs: Mock(
        task="fake.scan",
        args=args,
        kwargs=kwargs,
    )

    tasks, assigned = load_balancer_group_pks_by_auth(
        task,
        RoomItem.objects.all().order_by("pk"),
        [],
        {"token": "token", "max_msgs": 100},
    )

    assert len(tasks) == 2
    assert set(assigned) == {first_room.pk, second_room.pk}
    assigned_batches = {call.args[0][0] for call in task.s.call_args_list}
    assert assigned_batches == {first_room.pk, second_room.pk}
    assert all(call.kwargs["token"] == "token" for call in task.s.call_args_list)


@pytest.mark.django_db
def test_group_load_balancer_does_not_schedule_invalid_credentials(
    room_factory,
    telegram_auth_factory,
):
    invalid_auth = telegram_auth_factory(is_valid=False)
    room_factory(access_auth=invalid_auth)
    task = Mock()

    tasks, assigned = load_balancer_group_pks_by_auth(
        task,
        RoomItem.objects.all(),
        [],
        {},
    )

    assert tasks == []
    assert assigned == []
    task.s.assert_not_called()

