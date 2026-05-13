from django.utils import timezone
from django.conf import settings
from atenea_api.settings.services import BaseServiceConfig
from app_base.tagger import tag_hashtag
from urllib.parse import urlparse
from aiohttp.web_exceptions import HTTPException
from aiohttp.client_exceptions import ClientOSError, ClientResponseError, ContentTypeError
from app_base.models import LanguageField, GeneralEncoder
from bs4 import BeautifulSoup
from markdown import markdown
#import gcld3 
#import fasttext # Without support and incompatibility with NumPy 2.0
import eld
import asyncio
import hashlib
import dateutil.parser
import json
import logging
import requests
import aiohttp
import time
import re


# Get an instance of a logger
logger = logging.getLogger(__name__)

# Load lang detect model
#LANG_DETECTOR = gcld3.NNetLanguageIdentifier(min_num_bytes=12, max_num_bytes=15000)
# # Without support and incompatibility with NumPy 2.0
#LANG_DETECTOR = fasttext.load_model(os.path.join(settings.MODELS_DIR, 'lid.176.bin'))
LANG_DETECTOR = eld.LanguageDetector()

# ===============================================================
# ========             BASE UTILS FUNCTIONS              ========
# ===============================================================

def hash_json(input_json, excluding_keys=[]):
    """Calculate the hash value of a dictionary.

    Parameters
    ----------
    input_json: string
        Dictionary which you want to retrieve its hash value

    excluding_keys: list
        List of keys from the previous dictionary to which don' have to participate
        in the hash_value calculation.

    Returns
    -------
    hash_value: string
        Hash_value hexadecimal string.
    """

    aux_json = input_json
    if excluding_keys:
        aux_json = {
            k: v for k, v in input_json.items() if k not in excluding_keys
        }

    return hashlib.sha256(json.dumps(aux_json).encode("utf-8")).hexdigest()


def clean_link(link, remove_query_params=True):
    """Clean a link (optional: removing queryset params and # indexes).

    Many webs
    """

    if not link:
        return None

    link = link.lower()

    # Remove query params from get f.e: ...?param=1&param2=hola&param3=gg
    if remove_query_params:
        link = link.split("?")[0]

    # Remove '#' (used to search something in the web page, acting as a index)
    link = link.split("#")[0]

    return link


def link_to_unique(
    link, already_clean=False, remove_query_params=True, get_redirect=False
):
    """Standardized a link to obtain an unique string.

    This method is used to retrieve the same string despite receiving the same link
    with variations.

    F.e:
        https://es.test.com/hola
        http://es.test.com/hola
        https://es.test.com/hola/
        https://es.test.com/hola?query=campaing&h=ff

    All this urls should retrieve the same webpage, this method is used to standarize
    them and avoid duplicate elements:
        - standarized link: "es.test.com_hola"

    IMPORTANT! This url structure happens in the most newspapers and review sites
    (I haven't found any example of a newspaper/review site which use queryparams
    to switch the content). However, this have to be studied in each case, for example,
    youtube.com video urls use queryparam ?v=12345456 to choose the video (although,
    it have a equivalent url without query params youtu.be/123456).

    Parameters
    ----------
    link: string
        Target link to be standardized

    remove_query_params: boolean
        True by default. In this case the unique link is calculated without removing
        them.

    get_redirect: boolean
        True in case the desired link is a redirection of the previous parameter

    Returns
    -------
    unique_str: string
        Standarized string of a link.
    """

    if not link:
        return None

    if already_clean:
        link = clean_link(link, remove_query_params=remove_query_params)

    if get_redirect:
        link = requests.get(url=link).url

    up = urlparse(link)

    # Standarize HTTP protocol (removing the protocol for the hash)
    # Usually a http://url will be redirected to a https://url
    unique_str = up.netloc + up.path

    # Standarize url (removing last '/' and replacing it with '_' )
    # A url ending with '/' shouldn't return a different webpage if we remove the '/'
    # it can return an error but not a different page, so its string are equivalents.
    if unique_str.endswith("/"):
        unique_str = unique_str[:-1]
    unique_str = unique_str.replace("/", "_")

    return unique_str


def convert_to_standard_text(formatted_text):
    """ Remove formatted (bold, italic, etc...) text to standard plain one.

    NOTE: 'unidecode' library isn't used because it also convert unicode characters 
    from Russian, Chinese, etc.. to standard unicode. For example:
        - 每个人都有他的作战策略，直到脸上中了一拳。-> Mei Ge Ren Du You Ta De Zuo Zhan...
        (Same in Russian, Japanese, etc...)
    This is a problem because language detectors or NER extractors stop working.

    The solution is to create our own method that will only convert specific unicode
    ranges. You can remove/add new ranges in the `settings>base.py` `'RANGES_OFFSETS'`
    used by `UNICODE_TO_STANDARD_MAP` dict. 
    
    This variables are stored in settings in order to pre-process the dict and 
    improve the performance:
        - 5 secs for 423 chars * 100000 (without pre-processing dict)
        - 1.5 secs for 423 chars * 100000 reps (with pre-processing dict)

    This dict (`UNICODE_TO_STANDARD_MAP`) only takes 18.08KB in memory, it is worth it.

    Parameters
    ----------
    formatted_text: string
        Text to remove its formatting.
    
    Returns
    -------
    text: str
    """
    return ''.join(
        settings.UNICODE_TO_STANDARD_MAP.get(ord(char), char) 
        for char in formatted_text
    )


def markdown_to_text(markdown_string):
    """ Converts a markdown string to plaintext 
    
    - Transforms markdown links to 'Text: URL' format.

    Parameters
    ----------
    markdown_string: string
        Text to clean.
    
    Returns
    -------
    text: str
    """

    # First of all remove #hashtags to avoid them from being parsed (sometimes).
    mk_string_no_h, hashtags = tag_hashtag(markdown_string, replacement="!!!HT")

    # md -> html -> text since BeautifulSoup can extract text cleanly
    html = markdown(mk_string_no_h)
    # remove code snippets
    html = re.sub(r'<pre>(.*?)</pre>', ' ', html)
    html = re.sub(r'<code>(.*?)</code >', ' ', html)
    # parse html
    soup = BeautifulSoup(html, "html.parser")
    # Handle links specifically
    for a in soup.findAll('a'):
        if a.string:
            link_text = a.string.replace('\n', '')
            a.replace_with(f"{link_text}: {a.get('href', '')} ")
        else:
            a.replace_with(a.get('href', ''))
    # extract text
    text = ''.join(soup.findAll(text=True))

    # Reinsert hashtags in the place of placeholders
    for h in hashtags:
        text = text.replace("!!!HT", h, 1)
    return text


def detect_lang(text, max_length=LanguageField.MAX_ALLOWED_LENGTH, threshold=0.15):
    """Detect language of a text.

    Parameters
    ----------
    text: string
        Target text to detect its language.

    max_length: int, default=LanguageField.MAX_ALLOWED_LENGTH
        This param is used to control the returned lang length, if lang > max_length
        the returned lang will be "".
        
        NOTE: LanguageDetect lib return ISO 639-1 codes, some of them (zh-cn, zh-tw, 
        scn, tyv, yue, etc.) have more caracters than the accepted from LanguageField. 
        If this function is used to fill a LanguageField, the default value should 
        be left.

    threshold: float, default=0.15
        A threshold between 0-1 to ignore results below it

     Returns
    -------
    lang: str
        Language ISO 639-1 code or "" in case of error.
    """
    # Avoid using the model in case of empty str
    if not text.strip(): 
        return ""
    
    try:
        
        # fasttext lang detect doesn't allow \n 
        #labels, probabilities = LANG_DETECTOR.predict(text=text.strip().replace("\n", " "))

        # eld
        detection = LANG_DETECTOR.detect(text=text)

        # fasttext: if probabilities[0] < threshold:
        #if not detection.is_reliable(): # reliable doesn't work correctly
        #    logger.info(
        #        f"Detection ({detection.language}) is not reliable. Text: {repr(text)}"
        #    )
        #    lang = ""
        if detection.scores()[detection.language] < threshold:
            logger.debug(
                f"Detection ({detection.language}) doesn't have enough probability:{detection.scores()[detection.language]:.3f} min:{threshold}. Text: {repr(text)}"
            )
            lang = ""
        elif len(detection.language) > max_length:
            logger.debug(
                f"Not ISO-639-1: Not supported language - ({lang}) for text: {repr(text)}"
            )
            lang = ""
        # fasttext: Extract lang code from label '__label__es'
        # result = re.search("__label__(.*)", labels[0])
        # lang = result.group(1).split("-")[0] # In case of -> 'zh-cn' -> 'zh' 
        else:
            lang = detection.language

    except Exception as e:
        logger.debug(
            f"{e.__class__.__name__}: Unable to detect the language of the text {repr(text)} - {e}"
        )
        lang = ""

    return lang


def parse_str_to_datetime(date_string):
    try:
        # Parse date
        date = dateutil.parser.parse(date_string)
        # Make aware
        if not timezone.is_aware(date):
            date = timezone.make_aware(date)
        return date
    except Exception as e:
        logger.warning(f"{e.__class__.__name__}: {e}")
        return None


async def async_session_cpm(
    service_url, 
    session, 
    data, 
    bound_pk=None, 
    default_return={},
):
    """Asynchronous version using an existing session (to avoid creating a new one
    for each new request). Create a request to ask one of the REST APIs models for 
    a prediction using an existing session.

    Parameters
    ----------
    service_url: string
        URL from the REST API model.

    session: aiohttp.ClientSession
        aiohttp.ClientSession which is going to be used to make the request.

    data: str
        String with the request parameters. (urllib.urlencode or json.dumps)

    bound_pk: string, default=None
        Optional primary key to bind the result with an unique id. It will added
        in the returned dict. 

    default_return: list
        Default return in case of an error.

    Returns
    -------
    ret: list
        List with the JSON result from each request to the service.
    """

    try:
        start = time.time()
        async with session.post(
            service_url, 
            data=data, 
            headers={"Accept": "application/json"},
        ) as resp:  
            result = await resp.json()
            if bound_pk:
                result = {"pk": bound_pk, "result":result}

            logger.debug(
                f"Request to '{service_url}' completed [{resp.status}]. Total time: {(time.time() - start):.3f} secs."
            )
            return result
    except ClientResponseError as e:
        data = json.dumps(data)
        logger.warning(
            f"{e.__class__.__name__}: ({service_url})({e.message} - {e.status}) {data[:100]} [{len(data)} characters]..."
        )
    except (
        asyncio.exceptions.TimeoutError,
        ClientOSError,
        ContentTypeError,
        HTTPException,
    ) as e:
        data = json.dumps(data)
        logger.warning(
            f"{e.__class__.__name__}: ({service_url}) {data[:100]} [{len(data)} characters]... {e}"
        )
    
    return default_return


async def call_service(
    data: list[dict],
    service_config: BaseServiceConfig,
    endpoint_name: str,
    payload_builder_func: callable,
    payload_builder_kwargs: dict = {},
    payload_iterate_func: callable = None,
    max_by_request: int = None,
    max_parallel_requests: int = None,
    batch_size: int = 100,
) -> list[dict]:
    """ Base function for making batched requests to external services with async 
    parallelization.

    Parameters
    ----------
    data: list[dict]
        List of input items to process. Each dict should contain the necessary text fields.
        Example structure: [{"text_to_process": "..."}, ...]

    service_config: BaseServiceConfig
        Service configuration object from Django settings. Expected structure:
        ```python
        BaseServiceConfig(
            host="http://service.url/base",
            auth=ServiceAuthConfig(
                api_key="your_api_key",
                auth_header="AuthorizationHeaderName",
            ),
            endpoints={
                "endpoint_name": ServiceEndpointConfig(
                    path="/endpoint/path",
                    response_label="results_label",
                )
            },
            max_items_by_request=32,
            max_parallel_requests=8,
            default_return_format=dict,
        )
        ```

    endpoint_name: str
        Key name in `service_config.endpoints` that identifies the target endpoint

    payload_builder_func: callable
        Function that constructs the complete request payload for a chunk of data.
        Signature: (chunk: list[dict], **kwargs) -> dict

    payload_builder_kwargs: dict, default={}
        Keyword arguments to pass to `payload_builder_func`

    payload_iterate_func : callable, default=None
        Custom function to iterate over the input data and construct payloads for batched requests.

        This function should accept the following parameters:
            - data: list[dict]
                The input items to process.
            - max_by_request: int
                Maximum number of items to include in each batch or request.
            - builder: callable
                Function that constructs the payload from a batch of data. Its signature should be:
                (chunk: list[dict], **kwargs) -> dict
            - builder_kwargs: dict
                Additional keyword arguments to pass to the builder function.

        The function should return a list of payloads (list[dict]). If set to None, the default function is used,
        which iterates over the input data in sequential batches of size `max_by_request` and applies the builder
        function to each batch to create the corresponding payload.        

    max_by_request: int, default=None
        Maximum items per request. Uses service_config value if None

    max_parallel_requests: int, default=None
        Maximum parallel connections. Uses service_config value if None

    batch_size: int, default=100
        Maximum number of requests per session (controls session recycling)

    Returns
    -------
    list[dict]
        List of responses from each bached request. Each dict contains the result
        for each input item. None values will be present for failed items.
    """

    def default_payload_iterate_func(data, max_by_request, builder, builder_kwargs):
        """ Default function for iterating over data: 
            - In secuential batches and constructing payloads for each batch.

        Parameters
        ----------
        data : list[dict]
            List of input items to process. Each dictionary should contain the necessary fields.
            Example structure: [{"text": "Text to process", ...}, ...]

        max_by_request : int
            Maximum number of items to include in each batch or request.

        builder : callable
            Function that constructs the payload from a batch of data. 
            Its signature should be: (chunk: list[dict], **kwargs) -> dict

        builder_kwargs : dict, default={}
            Dictionary of additional keyword arguments passed to the `builder` function.

        Returns
        -------
        list[dict]
            List of constructed payloads, one for each batch of data.
        """
        payloads = []
        for idx in range(0, len(data), max_by_request):
            chunk = data[idx:idx + max_by_request]
            payloads.append(builder(chunk, **builder_kwargs))
        return payloads


    # Configure service parameters
    max_by_request = max_by_request or service_config.max_items_by_request
    max_parallel_requests = max_parallel_requests or service_config.max_parallel_requests

    # Build endpoint URL and headers
    endpoint = service_config.endpoints[endpoint_name]
    url = service_config.host + endpoint.path
    headers = {
        "Content-Type": "application/json",
        service_config.auth.auth_header: service_config.auth.api_key
    }
    reponse_results_label = endpoint.response_label

    # Default return type when a request fails: list or dict
    default_return_format = service_config.default_return_format

    # Use custom iterate func or default one instead
    payload_iterate_func = payload_iterate_func or default_payload_iterate_func

    # Prepare request payloads (`max_by_request` items by each one)
    payloads_pool = payload_iterate_func(
        data, 
        max_by_request, 
        payload_builder_func, 
        payload_builder_kwargs
    )

    # Execute batched requests
    tout = aiohttp.ClientTimeout(total=settings.MAX_TIMEOUT_SERVICES)
    responses = []
    
    for idx in range(0, len(payloads_pool), batch_size):
        # Create new session
        async with aiohttp.ClientSession(
            # Connector can't be reused between multiple sessions 
            # (it will reuse the same session leading to a 'RuntimeError: Session is closed')
            connector=aiohttp.TCPConnector(limit=max_parallel_requests),
            timeout=tout,
            raise_for_status=True,
            headers=headers
        ) as session:
            
            # Create and execute async tasks
            tasks = [
                async_session_cpm(
                    service_url=url,
                    session=session,
                    data=json.dumps(json_data, cls=GeneralEncoder), 
                    # In case of error return the same amount of outputs.
                    # The idea is that the input data has an output partner, 
                    # in case of error, that partner will be a None.
                    default_return=(
                        [None] * len(json_data["data"])
                        if default_return_format is list
                        else {reponse_results_label: [None] * len(json_data["data"])}
                    )
                )
                for json_data in payloads_pool[idx:idx+batch_size]
            ]

            start = time.time()
            responses.extend(await asyncio.gather(*tasks))
            logger.debug(
                f"Batch {(idx//batch_size)+1}/{(len(payloads_pool)//batch_size) + 1} completed. "
                f"URL: {url} | Total time: {(time.time() - start):.3f} secs."
            )
    
    return responses


class AsyncTimedIterable:
    """
    An asynchronous iterable that supports timeouts for asynchronous iterators.

    This utility can be used to wrap an existing asynchronous iterable and
    enforce a timeout on each iteration. If an iteration exceeds the specified
    timeout, a sentinel value is returned instead.

    Credits: https://stackoverflow.com/a/50245879

    Parameters
    ----------
    iterable: AsyncIterable
        The asynchronous iterable to wrap.
    timeout: float, default=None
        The maximum time (in seconds) to wait for each iteration. If an iteration
        takes longer than this value, the sentinel value is returned. 
    sentinel: Any, default=None
        The value to return if an iteration times out. 

    Examples
    --------
    ```python
    >>> import asyncio
    >>> async def async_gen():
    ...     for i in range(10):
    ...         await asyncio.sleep(1)
    ...         yield i
    ...
    >>> timed_iterable = 
    >>> async def AsyncTimedIterable(async_gen(), timeout=0.5, sentinel=-1)():
    ...     async for item in timed_iterable:
    ...         if item == -1:
    ...             break
    ...         print(item)
    ...
    >>> asyncio.run(consume_iterable())
    -1
    ```

    Note
    ----
    This class can be particularly useful when working with libraries like
    Telethon where you might want to enforce timeouts on async operations. To 
    avoid blocking events.

    """
    def __init__(self, iterable, timeout=None, sentinel=None):
        class AsyncTimedIterator:
            """
            An asynchronous iterator that supports timeouts.

            This inner class wraps an asynchronous iterator and enforces
            timeouts on its `__anext__` method. If an iteration exceeds the
            specified timeout, the sentinel value is returned.

            Methods
            -------
            __anext__()
                Returns the next item in the asynchronous iterator or the
                sentinel value if the operation times out.
            """
            def __init__(self):
                self._iterator = iterable.__aiter__()

            async def __anext__(self):
                try:
                    return await asyncio.wait_for(self._iterator.__anext__(),
                                                  timeout)
                except asyncio.TimeoutError:
                    return sentinel

        self._factory = AsyncTimedIterator

    def __aiter__(self):
        return self._factory()
