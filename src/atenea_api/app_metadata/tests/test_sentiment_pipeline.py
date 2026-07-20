from unittest.mock import AsyncMock, patch

import pytest

from app_metadata.services.sentiments import (
    call_sentiment_service,
    classify_sentiment,
    clean_text_to_sentiment,
    run_sentiment,
)


def test_clean_text_to_sentiment_removes_external_noise():
    cleaned = clean_text_to_sentiment("Hello @alice https://example.org test@example.org")
    assert "@alice" not in cleaned
    assert "https://" not in cleaned
    assert "test@example.org" not in cleaned


def test_call_sentiment_service_flattens_batches_and_keeps_failures():
    responses = [
        {"model": "demo", "version": "1", "sentiments": [2, None]},
        {"model": "demo", "version": "1", "sentiments": [0]},
    ]
    with patch(
        "app_metadata.services.sentiments.call_service",
        new=AsyncMock(return_value=responses),
    ):
        result = __import__("asyncio").run(
            call_sentiment_service(
                [
                    {"text_to_sentiment": "positive"},
                    {"text_to_sentiment": "failed"},
                    {"text_to_sentiment": "negative"},
                ],
                max_by_request=2,
                max_parallel_requests=1,
            )
        )

    assert result == ("demo", "1", [2, None, 0])


def test_run_sentiment_excludes_empty_texts_from_orchestration():
    with patch(
        "app_metadata.services.sentiments.generic_celery_task_orchestrator"
    ) as orchestrator:
        run_sentiment(
            None,
            "MessageItem",
            "app_telegram",
            "text",
            "sentiment",
            "sentiment_model",
        )

    assert orchestrator.call_args.kwargs["exclude_filter_fields"] == {"text__exact": ""}


@pytest.mark.django_db(transaction=True)
def test_classify_sentiment_updates_successes_and_skips_failed_items(
    message_factory,
    room_factory,
):
    room = room_factory(unique_name="sentiment_room", tg_id=20001)
    first = message_factory(
        room=room,
        msg_id=1,
        link="https://t.me/sentiment_room/1",
        text="Good result",
    )
    second = message_factory(
        room=room,
        msg_id=2,
        link="https://t.me/sentiment_room/2",
        text="Provider failure",
    )

    with patch(
        "app_metadata.services.sentiments.call_sentiment_service",
        new=AsyncMock(return_value=("sentiment-model", "1.0", [2, None])),
    ):
        classify_sentiment(
            [first.pk, second.pk],
            "MessageItem",
            "app_telegram",
            "text",
            "sentiment",
            "sentiment_model",
            clean_text=True,
            chunk_size=2,
            thread_count=1,
        )

    first.refresh_from_db()
    second.refresh_from_db()
    assert first.sentiment == 2
    assert first.sentiment_model == "sentiment-model_1.0"
    assert second.sentiment is None
    assert second.sentiment_model is None
