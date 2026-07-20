from django.db.models.functions import Trunc
from django.db.models import Count
import numpy as np
from app_telegram.models import MessageItem
from app_telegram.services.api import _filter_messages
from app_telegram.serializers import ANY as TAG_ANY

def get_message_stats(
    createdat_min=None, 
    createdat_max=None, 
    group_by='day', 
    room=None, 
    is_reply=None, 
    stored_since=None,
    tags=None,
    tag_match=TAG_ANY,
    z_score=False
):
    """
    Calculates message frequency statistics based on a given period and filters.

    Parameters
    ----------
    createdat_min: datetime.date, optional
        Interval start date for the `created_at` field.
    createdat_max: datetime.date, optional
        Interval end date for the `created_at` field.
    group_by: str, optional
        Period to group messages by ('day', 'week', 'month', 'year').
    room: List[str], optional
        List of room unique names to filter by.
    is_reply: bool, optional
        Filter messages that are replies.
    stored_since: datetime.date, optional
        Interval start date for the `stored_date` field.
    tags: List[str], optional
        List of room tags to filter by.
    tag_match: str, default="any"
        Determines if rooms should match all given tags or any of them.
    z_score: bool, optional
        If True, calculates the Z-Score for each group instead of the frequency.

    Returns
    -------
    Union[QuerySet, List[Dict]]
        A queryset of dictionaries with 'period_start' and 'count', or a list
        of dictionaries including 'z_score' if z_score is True.
    """
    qs = MessageItem.objects.all()

    # Apply common filters from app_telegram
    qs = _filter_messages(
        qs, 
        createdat_min=createdat_min, 
        createdat_max=createdat_max, 
        room=room, 
        is_reply=is_reply, 
        stored_since=stored_since,
        tags=tags,
        tag_match=tag_match,
    )

    # Group by the specified period and count messages
    stats = (
        qs.annotate(period_start=Trunc('created_at', group_by))
        .values('period_start')
        .annotate(count=Count('id'))
        .order_by('period_start')
    )

    if z_score:
        stats_list = list(stats)
        counts = [s['count'] for s in stats_list]

        if len(counts) < 2:
            for s in stats_list:
                s['z_score'] = 0.0
            return stats_list

        mean_count = np.mean(counts)
        std_dev_count = np.std(counts)

        if std_dev_count == 0:
            for s in stats_list:
                s['z_score'] = 0.0
            return stats_list

        for s in stats_list:
            s['z_score'] = (s['count'] - mean_count) / std_dev_count
        
        return stats_list

    return stats
