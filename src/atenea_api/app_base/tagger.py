import re
import itertools
from emoji import replace_emoji,demojize
from emoji.tokenizer import EmojiMatch, tokenize


def _tagger(text, pattern, replacement, with_offsets=False):
    """
    Replace and extract elements from a given text based on a specified pattern.

    This is a generic, private function designed with the common functionality.

    It can also provide the start and end offsets of each matched element in the
    original text if required.

    Parameters
    ----------
    text: str
        The text from which elements matching the pattern are to be identified and tagged.
    pattern: str
        The regular expression pattern used to identify elements in the text.
    replacement: str
        The string to replace each element found in the text that matches the pattern.
    with_offsets: bool, default=False
        If True, the function also returns the start and end offsets of each matched element. Defaults to False.

    Returns
    -------
    modified_text: str
        The text after elements matching the pattern have been replaced with the specified replacement string.
    extracted_elements: list[str | dict]
        If with_offsets is False: A list of elements that were extracted from the text based on the pattern.
        If with_offsets is True: A list of dictionaries, each containing 'match', 'start_offset', and 'end_offset' keys
        for each extracted element.
        NOTE: The start/end offsets belong to the original text.
    """

    matches = []

    def replace_func(match):
        element = match.group(0)
        if with_offsets:
            match_info = {
                'match': element.strip().replace("\n",""),
                'start_offset': match.start(),
                'end_offset': match.end()
            }
            matches.append(match_info)
        else:
            matches.append(element)
        return replacement

    modified_text = re.sub(pattern, replace_func, text)
    return modified_text, matches 


def tag_email(text, replacement='EMAIL', with_offsets=False):
    """
    Replace and extract email addresses from a given text.

    Parameters
    ----------
    text: str
        The text from which email addresses are to be identified and tagged.
    replacement: str, default="EMAIL"
        The string to replace each email address found in the text.
    with_offsets: bool, default=False
        If True, the function also returns the start and end offsets of each matched element. Defaults to False.

    Returns
    -------
    modified_text: str
        The text after email addresses have been replaced with the specified replacement string.
    extracted_emails: list[str | dict]
        If with_offsets is False: A list of emails that were extracted from the text based on the pattern.
        If with_offsets is True: A list of dictionaries, each containing 'match', 'start_offset', and 'end_offset' keys
        for each extracted emails.
        NOTE: The start/end offsets belong to the original text.
    """

    modified_text, emails = _tagger(
        text=text, 
        pattern=r'[a-zA-Z0-9._%+-]+@[a-zA-Z0-9.-]+\.[a-zA-Z]{2,}', 
        replacement=replacement,
        with_offsets=with_offsets
    )
    return modified_text, emails


def tag_url(text, replacement='URL', with_offsets=False):
    """
    Replace and extract URLs from a given text.

    Parameters
    ----------
    text: str
        The text from which URLs are to be identified and tagged.
    replacement: str, default="URL"
        The string to replace each URL found in the text.
    with_offsets: bool, default=False
        If True, the function also returns the start and end offsets of each matched element. Defaults to False.

    Returns
    -------
    modified_text: str
        The text after URLs have been replaced with the specified replacement string.
    extracted_urls: list[str | dict]
        If with_offsets is False: A list of URLs that were extracted from the text based on the pattern.
        If with_offsets is True: A list of dictionaries, each containing 'match', 'start_offset', and 'end_offset' keys
        for each extracted URLs.
        NOTE: The start/end offsets belong to the original text.
    """

    modified_text, urls = _tagger(
        text=text, 
        pattern='https?://\n?[^\s\)]+', 
        replacement=replacement,
        with_offsets=with_offsets
    )
    return modified_text, urls


def tag_userref(text, replacement='REF', with_offsets=False):
    """
    Replace and extract user references (e.g., @username) from a given text.

    Parameters
    ----------
    text: str
        The text from which user references are to be identified and tagged.
    replacement: str, default="REF"
        The string to replace each user reference found in the text.
    with_offsets: bool, default=False
        If True, the function also returns the start and end offsets of each matched element. Defaults to False.

    Returns
    -------
    modified_text: str
        The text after user references have been replaced with the specified replacement string.
    extracted_userrefs: list[str | dict]
        If with_offsets is False: A list of userrefs that were extracted from the text based on the pattern.
        If with_offsets is True: A list of dictionaries, each containing 'match', 'start_offset', and 'end_offset' keys
        for each extracted userref.
        NOTE: The start/end offsets belong to the original text.
    """

    modified_text, userrefs = _tagger(
        text=text, 
        pattern='(?<!\w)@\w+', 
        replacement=replacement,
        with_offsets=with_offsets
    )
    return modified_text, userrefs


def tag_hashtag(text, replacement='TAG', with_offsets=False):
    """
    Replace and extract hashtags (e.g., #hashtags) from a given text.

    Parameters
    ----------
    text: str
        The text from which hashtags are to be identified and tagged.
    replacement: str, default="TAG"
        The string to replace each hashtag found in the text.
    with_offsets: bool, default=False
        If True, the function also returns the start and end offsets of each matched element. Defaults to False.

    Returns
    -------
    modified_text: str
        The text after hashtags have been replaced with the specified replacement string.
    extracted_hashtags: list[str | dict]
        If with_offsets is False: A list of hashtags that were extracted from the text based on the pattern.
        If with_offsets is True: A list of dictionaries, each containing 'match', 'start_offset', and 'end_offset' keys
        for each extracted hashtag.
        NOTE: The start/end offsets belong to the original text.
    """

    modified_text, hashtags = _tagger(
        text=text, 
        pattern='#[\w]+', 
        replacement=replacement,
        with_offsets=with_offsets
    )
    return modified_text, hashtags


def tag_date(text, replacement='DAT', with_offsets=False):
    """
    Replace and extract dates (f.e XX/NN/YYYY or XX/NN) from a given text.

    # rationale: a date is a two or three blocks of digits separated by a slash.
    
    Parameters
    ----------
    text: str
        The text from which dates are to be identified and tagged.
    replacement: str, default="DAT"
        The string to replace each date found in the text.
    with_offsets: bool, default=False
        If True, the function also returns the start and end offsets of each matched element. Defaults to False.

    Returns
    -------
    modified_text: str
        The text after dates have been replaced with the specified replacement string.
    extracted_dates: list[str | dict]
        If with_offsets is False: A list of dates that were extracted from the text based on the pattern.
        If with_offsets is True: A list of dictionaries, each containing 'match', 'start_offset', and 'end_offset' keys
        for each extracted date.
        NOTE: The start/end offsets belong to the original text.
    """

    modified_text, dates = _tagger(
        text=text, 
        pattern='[0-9]?[0-9][-/][0-9]?[0-9]([-/][0-9][0-9][0-9][0-9])?', 
        replacement=replacement,
        with_offsets=with_offsets
    )
    return modified_text, dates


def tag_time(text, replacement='TIM', with_offsets=False):
    """
    Replace and extract times from a given text.

    f.e:
    > 24-hour format: 23:59, 00:00, 14:30
    > 12-hour format with AM/PM: 11:59 PM, 12:00 AM, 1:30 PM
    
    Parameters
    ----------
    text: str
        The text from which times are to be identified and tagged.
    replacement: str, default="TIM"
        The string to replace each time found in the text.
    with_offsets: bool, default=False
        If True, the function also returns the start and end offsets of each matched element. Defaults to False.

    Returns
    -------
    modified_text: str
        The text after times have been replaced with the specified replacement string.
    extracted_times: list[str | dict]
        If with_offsets is False: A list of times that were extracted from the text based on the pattern.
        If with_offsets is True: A list of dictionaries, each containing 'match', 'start_offset', and 'end_offset' keys
        for each extracted time.
        NOTE: The start/end offsets belong to the original text.
    """

    modified_text, times = _tagger(
        text=text, 
        pattern='\b(?:[01]?\d|2[0-3]):[0-5]\d(?:\s?[AP]M)?', 
        replacement=replacement,
        with_offsets=with_offsets
    )
    return modified_text, times


def tag_number(text, replacement='NUM', with_offsets=False):
    """
    Replace and extract numbers from a given text.

    f.e:
    > Simple integers:
        - 123
        - 4567
    > Large integers without separators:
        - 1234567890
        - 987654321
    > Numbers with thousands separators (either commas or periods, depending on the locale):
        - 1,234
        - 1.234.567
    > Decimal numbers with a dot or comma as the decimal separator:
        - 123.45
        - 6789,01
    > Numbers with a positive or negative sign:
        +1234
        -5678
    
    Parameters
    ----------
    text: str
        The text from which numbers are to be identified and tagged.
    replacement: str, default="NUM"
        The string to replace each nunber found in the text.
    with_offsets: bool, default=False
        If True, the function also returns the start and end offsets of each matched element. Defaults to False.

    Returns
    -------
    modified_text: str
        The text after numbers have been replaced with the specified replacement string.
    extracted_nums: list[str | dict]
        If with_offsets is False: A list of nums that were extracted from the text based on the pattern.
        If with_offsets is True: A list of dictionaries, each containing 'match', 'start_offset', and 'end_offset' keys
        for each extracted num.
        NOTE: The start/end offsets belong to the original text.
    """

    modified_text, nums = _tagger(
        text=text, 
        pattern='[+-]?\b\d{1,3}(?:[.,]\d{3})*(?:[.,]\d+)?\b|\b\d+(?:[.,]\d+)?\b', 
        replacement=replacement,
        with_offsets=with_offsets
    )
    return modified_text, nums


def tag_emoji(text, replacement='EMJ', with_offsets=False):
    """
    Replace and extract emojis from a given text.

    Parameters
    ----------
    text: str
        The text from which emojis are to be identified and tagged.
    replacement: str, default="EMJ"
        The string to replace each time found in the text.
    with_offsets: bool, default=False
        If True, the function also returns the start and end offsets of each matched element. Defaults to False.

    Returns
    -------
    modified_text: str
        The text after emojis have been replaced with the specified replacement string.
    extracted_emojis: list[str | dict]
        If with_offsets is False: A list of times that were extracted from the text based on the pattern.
        If with_offsets is True: A list of dictionaries, each containing 'match', 'start_offset', and 'end_offset' keys
        for each extracted emoji.
        NOTE: The start/end offsets belong to the original text.
    """

    def custom_emoji_list(text):
        """ Custom emoji.emoji_list(string) method.

        With custom keys ('match', 'start_offset', and 'end_offset') instead of
        ('emoji', 'match_start', and 'match_end').
        """
        return [{
            'match': demojize(m.value.emoji),
            'start_offset': m.value.start,
            'end_offset': m.value.end
        } for m in tokenize(text, keep_zwj=False) if isinstance(m.value, EmojiMatch)]

    emojis = custom_emoji_list(text) 
    if not with_offsets:
        emojis = [e['match'] for e in emojis]
    return replace_emoji(text, replacement), emojis


def tag_wallet_eth(text, replacement='WALLET_ETH', with_offsets=False):
    """
    Replace and extract Ethereum (ETH) wallet addresses from a given text.
    """
    modified_text, eths = _tagger(
        text=text, 
        pattern=r'\b0x[a-fA-F0-9]{40}\b', 
        replacement=replacement,
        with_offsets=with_offsets
    )
    return modified_text, eths


def tag_wallet_btc(text, replacement='WALLET_BTC', with_offsets=False):
    """
    Replace and extract Bitcoin (BTC) wallet addresses from a given text.
    """
    modified_text, btcs = _tagger(
        text=text, 
        pattern=r'\b(?:bc1|[13])[a-zA-HJ-NP-Z0-9]{25,39}\b', 
        replacement=replacement,
        with_offsets=with_offsets
    )
    return modified_text, btcs


def tag_wallet_dash(text, replacement='WALLET_DASH', with_offsets=False):
    """
    Replace and extract Dash (DASH) wallet addresses from a given text.
    """
    modified_text, dashs = _tagger(
        text=text, 
        pattern=r'\bX[1-9A-HJ-NP-Za-km-z]{33}\b', 
        replacement=replacement,
        with_offsets=with_offsets
    )
    return modified_text, dashs


def tag_wallet_xmr(text, replacement='WALLET_XMR', with_offsets=False):
    """
    Replace and extract Monero (XMR) wallet addresses from a given text.
    """
    modified_text, xmrs = _tagger(
        text=text, 
        pattern=r'\b4[0-9AB][1-9A-HJ-NP-Za-km-z]{93}\b', 
        replacement=replacement,
        with_offsets=with_offsets
    )
    return modified_text, xmrs


def remove_matches(text, matches):
    # Sort the matches in descending order by start_offset
    sorted_matches = sorted(matches, key=lambda x: x['start_offset'], reverse=True)
    
    # Iterate over the sorted matches and remove each from the text
    for match in sorted_matches:
        start = match['start_offset']
        end = match['end_offset']
        text = text[:start] + text[end:]
    
    return text


def extract_tags(
    text,
    email = True,
    url = True,
    userref = True,
    hashtag = True,
    date = True,
    time = True,
    number = True,
    emoji = True,
    wallet_eth = True,
    wallet_btc = True,
    wallet_dash = True,
    wallet_xmr = True,
):
    """
    Extract tags and remove them from a given text, such as:
        - emails, URLs, user references, hashtags, dates, times, numbers, and emojis.

    This function allows tagging and replacing different types of elements within the text, 
    providing the start and end offsets of each matched element.

    Parameters
    ----------
    text: str
        The text to be processed for tagging.
    email: bool, default=True
        If True, email addresses will be tagged.
    url: bool, default=True
        If True, URLs will be tagged.
    userref: bool, default=True
        If True, user references (e.g., @username) will be tagged.
    hashtag: bool, default=True
        If True, hashtags will be tagged.
    date: bool, default=True
        If True, dates will be tagged.
    time: bool, default=True
        If True, times will be tagged.
    number: bool, default=True
        If True, numbers will be tagged.
    emoji: bool, default=True
        If True, emojis will be tagged.
    wallet_eth: bool, default=True
        If True, Ethereum (ETH) wallet addresses will be tagged.
    wallet_btc: bool, default=True
        If True, Bitcoin (BTC) wallet addresses will be tagged.
    wallet_dash: bool, default=True
        If True, Dash (DASH) wallet addresses will be tagged.
    wallet_xmr: bool, default=True
        If True, Monero (XMR) wallet addresses will be tagged.

    Returns
    -------
    modified_text: str
        The text after processing, with elements tagged and optionally replaced as specified.
    matches: dict
        A dictionary where keys are element types (e.g., 'EMAIL', 'URL', etc.) and values are lists of 
        identified dictionaries, each containing 'match', 'start_offset', and 'end_offset' keys for each extracted element. 
    """

    matches = {}
    if email:
        _, matches["EMAIL"] = tag_email(text, "", with_offsets=True)
    if url:
        _, matches["URL"] = tag_url(text, "", with_offsets=True)
    if userref:
        _, matches["MENTION"] = tag_userref(text, "", with_offsets=True)
    if hashtag:
        _, matches["HASHTAG"] = tag_hashtag(text, "", with_offsets=True)
    if date:
        _, matches["DATE"] = tag_date(text, "", with_offsets=True)
    if time:
        _, matches["TIME"] = tag_time(text, "", with_offsets=True)
    if number:
        _, matches["NUM"] = tag_number(text, "", with_offsets=True)
    if emoji:
        _, matches["EMOJI"] = tag_emoji(text, "", with_offsets=True)
    if wallet_eth:
        _, matches["WALLET_ETH"] = tag_wallet_eth(text, "", with_offsets=True)
    if wallet_btc:
        _, matches["WALLET_BTC"] = tag_wallet_btc(text, "", with_offsets=True)
    if wallet_dash:
        _, matches["WALLET_DASH"] = tag_wallet_dash(text, "", with_offsets=True)
    if wallet_xmr:
        _, matches["WALLET_XMR"] = tag_wallet_xmr(text, "", with_offsets=True)

    # Modified text in each tag_* must be ignored. All the extracted entities
    # must be removed from text at once to avoid miscalculating offsets.
    # F.e: If I extract emails and then URLs, the offsets from URLs will have their
    # offset miscalculated because emails have not been taken into account.
    new_text = remove_matches(text, itertools.chain.from_iterable(matches.values()))
    return new_text, matches