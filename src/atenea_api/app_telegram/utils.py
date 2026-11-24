import re
from urllib.parse import urlparse

from app_telegram.models import BaseTelegramEntity


# ===============================================================
# ========       TELEGRAM APP UTILS FUNCTIONALITY        ========
# ===============================================================

def telegram_link_normalizer(link, rebuild_link = False):
    """Normalize a Telegram link (t.me) and returns the unique substring from it.

    This method is used to retrieve the same string despite of receiving variations
    of the same Telegram item.

    This function will retrieve the same string whatever the link received (as 
    long as it's functional and points to the same telegram element). 
    If the retrieved link is wrong it will return None.

    NOTE: Telegram invitation links (t.me/joinchat/...) keeps the 'joinchat/'
    despite not being unique

    NOTE: The returned string is still functional if the domain is appended to it:
        - https://t.me/<unique_str>

    e.g:
        - "http://t.me/binance_chain_official" 
        - "https://t.me/binance_chain_official",
        - "https://t.me/binance_chain_official?param=1", 
        - "https://t.me/binance_chain_official/1290",
        - Points to the same channel --> binance_chain_official

        - "https://t.me/JOINchat/Dm-Vwk9bRZb6m3sqqZlQfg",
        - "https://t.me/joinchat/Dm-Vwk9bRZb6m3sqqZlQfg/344" 
        - Points to the invitation code --> joinchat/Dm-Vwk9bRZb6m3sqqZlQfg

    (Invitational links will be lowered-case differently to prevent them from
    breaking)

    Parameters
    ----------
    link: string
        Telegram link from a Channel, Group, Bot or User (also invitation links)
        to be normalized. f.e: https://t.me/THE_BEST_CHANNEL.
        It must contain the t.me domain.

    rebuild_link: boolean, default=False
        If True, the function will rebuild the link from the unique substring. In
        case of False, the function will return only the unique substring.
        e.g:
        ```
        >>> telegram_link_normalizer("https://t.me/Best_of_Finance", True)
        "https://t.me/best_of_finance"
        >>> telegram_link_normalizer("https://t.me/Best_of_Finance", False)
        "best_of_finance"
        ```
        
    Returns
    -------
    unique_str: string
        Returns the unique normalized string from the telegram link. If it's an 
        invalid Telegram link it will return None. 
    """
    if not isinstance(link, str) or not link.strip():
        return None

    candidate = link.strip()
    # Keep accepting scheme-less t.me links, but parse them as URLs so the host
    # can be checked independently from the path.
    if "://" not in candidate:
        candidate = "https://" + candidate

    try:
        url_parsed = urlparse(candidate)
        hostname = (url_parsed.hostname or "").lower().rstrip(".")
    except ValueError:
        return None

    if url_parsed.scheme.lower() not in {"http", "https"}:
        return None
    if hostname not in {"t.me", "www.t.me"}:
        return None

    # Invitation tokens are case-sensitive and are not public usernames.
    if re.match(r"^/joinchat(?:/|$)", url_parsed.path, re.IGNORECASE):
        invitation = re.match(r"^/joinchat/([-\w]+)(?:/|$)", url_parsed.path, re.IGNORECASE)
        if not invitation:
            return None
        unique_path = "joinchat/" + invitation.group(1)
        return "https://t.me/" + unique_path if rebuild_link else unique_path

    match = re.match(rf"^/({BaseTelegramEntity.USERNAME_PATTERN})(?:/|$)", url_parsed.path)
    if not match:
        return None
    if rebuild_link:
        return BaseTelegramEntity.build_public_link(match.group(1))
    return BaseTelegramEntity.normalize_username(match.group(1))
