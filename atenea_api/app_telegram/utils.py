import re
from urllib.parse import urlparse


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
    url_parsed = urlparse(link.strip())
    parsed_link = url_parsed.netloc + url_parsed.path
    # In case of an invitation link 
    if "joinchat" in parsed_link.lower():
        # We can't do .lower() to the invitation code, but we want to force 'joinchat' to
        # be lowercase. 
        re_to_match = r't.me/joinchat/([\-\w]+)'
        extra = "joinchat/" 
    else:
        re_to_match = r't.me/(\w+)'
        extra = ""
        parsed_link = parsed_link.lower() # joinchat invitation code can't be set to lower

    match = re.search(re_to_match, parsed_link)
    if match:
        unique_path = extra + match.group(1)
        return "https://t.me/" + unique_path if rebuild_link else unique_path
    
    return None # Wrong link