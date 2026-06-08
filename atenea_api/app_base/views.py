import math
import logging
import uuid
from collections.abc import Iterable
from django.conf import settings
from django.core.exceptions import ValidationError as DjangoValidationError
from django.contrib.gis.geoip2 import GeoIP2
from rest_framework import request, pagination, serializers, status
from rest_framework.response import Response
from rest_framework.exceptions import NotFound, ValidationError
from app_base.models import Query
from app_base.serializers import GenericResponseSerializer, PKsSerializer
from app_users.models import User


# Get an instance of a logger
logger = logging.getLogger(__name__)


class BasePaginator(pagination.PageNumberPagination):
    """ Custom paginator to limit the number of accesible pages.
    """

    def __init__(self, page_size=10, is_authenticated = False, max_page_size = 100):
        self.page_size = page_size if page_size else 1  # Size of each page
        if is_authenticated:
            # Allow parameter to customize pages
            self.page_size_query_param = "page-size"
            self.max_page_size = max_page_size
            
            # Max pages for authenticated users
            self.max_pages = settings.REST_FRAMEWORK.get('MAX_PAGES_AUTHENTICATED', 1000) 
                
        else:
            self.max_pages = settings.REST_FRAMEWORK.get('MAX_PAGES_ANON', 100) 


    def paginate_queryset(self, queryset, request, view=None):
        
        # Check page parameter
        param_value = request.query_params.get(self.page_query_param, "1")
        if param_value.isdigit():
            page_number = int(param_value)
        else:
            msg = self.invalid_page_message.format(
                message="Error in parameter 'page', contains wrong values."
            )
            raise NotFound(msg)
        
        # Check max pages
        if page_number > self.max_pages:
            msg = self.invalid_page_message.format(
                page_number=page_number, message="Limit exceeded"
            )
            raise NotFound(msg)
        
        return super().paginate_queryset(queryset, request, view)


    def get_paginated_response(self, data, token=None):
        """
        We add the token to the response data, and also to the
        next/previous links.
        """
        return Response(
            {
                "next": self.get_next_link(),
                "previous": self.get_previous_link(),
                "count": self.page.paginator.count,
                "token": token,
                "results": data,
            }
        )


class LoggedValidationError(ValidationError):
    """ Custom ValidationError to add a Query token to be able to track the request
    that resulted in a validation error.
    """
    def __init__(self, detail=None, code=None, token=None):
        self.token = token  # Add custom identifier
        super().__init__(detail, code)


# ======================================================
# =====          BASE VIEWS FUNCTIONALITY          =====
# ======================================================

def get_and_log_request_data(
    request: request.Request,
    list_params: list = [],
    to_log_query: bool = True,
    hide_data_in_query: bool = False
):
    """Parse the request data and return a dictionary with all the parameters and
    create a Query item to log the request (if `to_log_query=True`).

    NOTE: This function handle Javascript-style params (with hyphens) and replace
    them into Python-style (snake_case). e.g: 
        - params: `?is-param=1&is-param=2&is-param=3`
        - result: `{"is_param": [1,2,3]}`

    Parameters
    ----------
    request: rest_framework.request.Request
        Request to parse.

    list_params: list, default=[]
        Name of the parameters that should be processed as a list. This is necessary
        for GET params or POST Form data.
        F.e: ...&param1=1&param1=2&param1=3 --> param1 = [1,2,3]
        
    to_log_query: boolean, default=True
        If True, the request will be logged in a Query item and the token will be 
        returned. If False, the token will be None.

    hide_data_in_query: boolean, default=False
        If True, the data will be hidden in the Query item. If False, the data will
        be logged in the Query item.

    Returns
    -------
    data: dict | list[dict]
        Return the data from the request (list of dicts or dict).

    token: uuid.UUID
        Return the token of the Query that logs the request. False if to_log_query=False.
    """
    # Handle read-like requests with query params.
    # DELETE endpoints in this API also use URL filters to select affected items.
    if request.method in {"GET", "DELETE"}:
        # Get all query params and handle list-type values
        data = {}
        for key, value in request.query_params.lists():
            # Assign lists if multiple values or the key is in list_params, otherwise 
            # assign the single value (taking the first one)
            data[key] = value if (len(value) > 1 or key in list_params) else value[0]

        # Allow query params to use hyphens
        # Replace Javascript-style params (with hyphens) into Python-style (snake_case)
        data = {k.replace("-", "_"): v for k, v in data.items()}
    else:
        # For body-based methods, get the data (POST, PUT, PATCH)
        data = request.data

    # Log this request (in a Query item)
    token = (
        log_query(request, data if not hide_data_in_query else "Hidden. Confidential data")
        if to_log_query else None
    )
    return data, token


def parse_request_data(
    request: request.Request,
    SerializerClass: serializers.BaseSerializer,
    extra_args: dict = {},
    to_log_query: bool = True,
    hide_data_in_query: bool = False
):
    """ A general request manager for the whole project that handles any kind of
    request and serializes its parameters in most formats. This function will return 
    a dictionary with all the parameters validated and cleaned ready to be used.

    Accepted format: 
        - GET: Params in the URL 
        - POST: JSON, Form, Form-encode
    
    NOTE: Params with composed names are separated by "-". This function changes
    them to snake case. Eg: num-items -> num_items. (Transforms front-end variable
    names into Python variable names)

    Parameters
    ----------
    request: rest_framework.request.Request
        Request to parse.

    SerializerClass: rest_framework.serializers.BaseSerializer
        Serializer to use for validate and clean the data.

    extra_args: dict, default={}
        Send extra keyword arguments to the Serializer (check in the rest_framework 
        package the Serializer documentation). For example: 
            - partial=True (to partially update an existing item)
            - instance=instance (to indicate that the serializer is being used to 
            update an existing item)
            - context={....} (e.g context: {"languages": ["en", "es"]} to add extra
            info https://www.django-rest-framework.org/api-guide/serializers/#including-extra-context

    to_log_query: boolean, default=True
        If True, the request will be logged in a Query item and the token will be 
        returned. If False, the token will be None.

    hide_data_in_query: boolean, default=False
        If True, the data will be hidden in the Query item. If False, the data will
        be logged in the Query item.

    Returns
    -------
    data: list[dict] | dict
        Return the validated data (list of dicts or dict). 
        NOTE: Any OrderedDict created by Django serializers will be converted to
        a normal dict.

    token: str
        Return the token of the Query that logs the request. False if log_query=False.

    Raises
    ------
    LoggedValidationError
        If the serializer is not valid. Contains the token of the Query that 
        logs the request.
    """
    
    list_params = [
        name 
        for name, field in SerializerClass().fields.items() 
        if isinstance(field, serializers.ListField)
    ]
    # First of all get data from the request and log it (by creating a Query item)
    data, token = get_and_log_request_data(
        request=request, 
        list_params=list_params,
        to_log_query=to_log_query, 
        hide_data_in_query=hide_data_in_query
    )

    # Serialize and validate the data
    serializer = SerializerClass(data=data, **extra_args)
    if not serializer.is_valid():
        raise LoggedValidationError(serializer.errors, token=token)

    # Transform data from OrderedDict to dict
    if isinstance(serializer.validated_data, list):
        return [dict(item) for item in serializer.validated_data], token
    return dict(serializer.validated_data), token


def manage_crud_request(
    request: request.Request,
    crud_action: str,
    ModelSerializerClass: serializers.ModelSerializer,
    item_pk: uuid.UUID | str = None,
    extra_args: dict = {},
    to_log_query: bool = True,
    hide_data_in_query: bool = False
):
    """ Manage CRUD generic requests `["create", "update", "destroy"]` for a Model.
    This function will extract the data from the request, log it (by creating a 
    Query item), then validate and clean the data, finally, create/update/delete
    the item.

    This function handles Django's ValidationError, RestFramework's ValidationError
    and Model.DoesNotExist exception.

    Parameters
    ----------
    request: rest_framework.request.Request
        Request to parse.
    
    crud_action: str
        CRUD action to perform. Available: "create", "update", "destroy".

    ModelSerializerClass: rest_framework.serializers.ModelSerializer
        Serializer to use for validate and clean the data and extract the Model.

    item_pk: uuid.UUID | str, default=None
        Primary key of the Model to be updated/deleted. 

    extra_args: dict, default={}
        Send extra keyword arguments to the Serializer (check in the rest_framework 
        package the Serializer documentation). For example: 
            - partial=True (to partially update an existing item)

    to_log_query: boolean, default=True
        If True, the request will be logged in a Query item.

    hide_data_in_query: boolean, default=False
        If True, the data will be hidden in the Query item. If False, the data will
        be logged in the Query item.

    Returns
    -------
    Response: rest_framework.response.Response
        A response using the GenericResponseSerializer data structure.
        Responses:
            - Status code: 201 | Body `results`: The created item.
            - Status code: 200 | Body `results`: The updated item.
            - Status code: 204 | Body `results`: None.
            - Status code: 400 | Body `results`: The errors.
            - Status code: 404 | Body `results`: None.
    Raises
    ------
    NotImplementedError
        If the CRUD action in `crud_action` is not implemented.
    """
    
    # First of all get Model 
    ModelClass = ModelSerializerClass.Meta.model

    list_params = [
        name 
        for name, field in ModelSerializerClass().fields.items() 
        if isinstance(field, serializers.ListField)
    ]
    # Then, get data from the request and log it (by creating a Query item)
    data, token = get_and_log_request_data(
        request=request, 
        list_params=list_params,
        to_log_query=to_log_query, 
        hide_data_in_query=hide_data_in_query
    )

    try:
        if crud_action in ["create", "update"]:
            item = ModelClass.objects.get(pk=item_pk) if item_pk else None
            # Serialize and validate the data
            serializer = ModelSerializerClass(
                data=data, 
                # to tell Django to update the existing instance. Avoiding UniqueValidationErrors in some fields
                instance=item, 
                **extra_args
            )
            
            # Raise if the serializer is not valid
            serializer.is_valid(raise_exception=True)
            # Create/update
            item = (
                serializer.create(validated_data=serializer.validated_data)
                if crud_action == "create" 
                else serializer.update(item, serializer.validated_data)
            )
            return create_paginate_response(
                token=token,
                queryset=[item], 
                request=request,
                serializer_class=ModelSerializerClass,
            )
        elif crud_action == "destroy":
            # Return 204 No Content regardless of whether the resource existed or not.
            # The state of the server resource is the same after the DELETE request.
            # Then, use filter to don't raise a DoesNotExist exception.
            item = ModelClass.objects.filter(pk=item_pk).first()
            if item:
                item.delete()
            return create_message(
                token, 
                status.HTTP_204_NO_CONTENT, 
            )
        else:
            raise NotImplementedError(f"The CRUD action '{crud_action}' is not implemented.")

    except ModelClass.DoesNotExist as e:
        logger.info(f"{ e.__class__.__name__ }: {e}")
        return create_message(
            token, 
            status.HTTP_404_NOT_FOUND, 
            "There is no element with that id."
        )
    except (ValidationError, DjangoValidationError) as e:
        logger.info(f"{ e.__class__.__name__ }: {e}")
        return create_message(
            token, 
            status.HTTP_400_BAD_REQUEST, 
            custom_message="There were some issues with the input data.",
            add_errors=e.detail if isinstance(e, ValidationError) else e.messages
        )


def manage_bulk_destroy_request(
    request: request.Request,
    ModelSerializerClass: serializers.ModelSerializer,
    extra_args: dict = {},
    to_log_query: bool = True,
    hide_data_in_query: bool = False
):
    """ Manage bulk delete requests. All this request must use PKsSerializer as input
    (the default serializer to handle a list of PKs).

    This function handles RestFramework's ValidationErrors. No errors about Model.DoesNotExist
    can be raised (filter is used instead of get). A delete request returns 204
    No Content regardless of whether the resource existed or not, this is due to the fact
    that the state of the server resource is the same after the DELETE request.

    Parameters
    ----------
    request: rest_framework.request.Request
        Request to parse.
    
    ModelSerializerClass: rest_framework.serializers.ModelSerializer
        Serializer to use for validate and clean the data and extract the Model.

    extra_args: dict, default={}
        Send extra keyword arguments to the Serializer (check in the rest_framework 
        package the Serializer documentation).
    
    to_log_query: boolean, default=True
        If True, the request will be logged in a Query item.

    hide_data_in_query: boolean, default=False
        If True, the data will be hidden in the Query item. If False, the data will
        be logged in the Query item.

    Returns
    ----------
    Response: rest_framework.response.Response
        A response using the GenericResponseSerializer data structure.
        Responses:
            - Status code: 204 | Body `results`: None.
            - Status code: 400 | Body `results`: The errors.
    """
    # First of all get Model 
    ModelClass = ModelSerializerClass.Meta.model
    try:
        data, token = parse_request_data(
            request=request,
            SerializerClass=PKsSerializer,
            extra_args=extra_args,
            to_log_query=to_log_query,
            hide_data_in_query=hide_data_in_query
        )
        pks = data["pks"]
        _, del_by_model = ModelClass.objects.filter(pk__in=pks).delete()
        logger.info(
            ",".join([f"{value} {key}" for key, value in del_by_model.items()]) +
            " items were deleted."
        )
        return create_message(
            token, 
            status.HTTP_204_NO_CONTENT
        ) 
    except LoggedValidationError as e:
        logger.info(
            f"{ e.__class__.__name__ }: {e.detail}",
        )
        return create_message(
            e.token, 
            status.HTTP_400_BAD_REQUEST, 
            "There were some issues with the input data.",
            add_errors=e.detail
        )


def log_query(
    request: request.Request,
    params: dict = None,
):
    """Create a new Query to log the request and return its primary key (token).
    In case of an unlogged requests the "owner" field will be set to None. 

    Returns
    -------
    token: string
        Return uuid.hex token.
    """

    # Remove the server prefix
    endpoint_url = request.build_absolute_uri("?").split("api/")[1]

    token = Query.objects.create(
        url=endpoint_url,
        location=get_loc(request),
        method=request.method,
        owner=request.user if isinstance(request.user, User) else None,
        data=params
    ).token.hex

    return token


def get_loc(request: request.Request):
    """Get request's location from its IP address."""

    g = GeoIP2("data/geoip")
    if "HTTP_X_FORWARDED_FOR" in request.META:
        x_forwarded_for = request.META.get("HTTP_X_FORWARDED_FOR")
        ip = x_forwarded_for.split(",")[0]
    else:
        ip = request.META.get("REMOTE_ADDR")
    if ip != "127.0.0.1":
        try:
            location = g.country(ip)["country_code"]
        except Exception as e:
            logger.warning(
                f"{e.__class__.__name__}: Unable to get the location from the ip '{ip}'."
            )
            location = "unknown"
    if ip == "127.0.0.1":
        location = "localhost"
    return location


def create_message(
    token: str | uuid.UUID,
    status_code: int,
    custom_message:str = None,
    add_results: dict | list = None,
    add_errors: dict | list = None
):
    """To create a response with generic structure. The message can be a standard 
    one for each status_code or customized.

    Parameters
    ----------
    token: string | uuid.UUID
        UUID.hex

    status_code: int
        HTTP status codes. You should use 2XX, 4XX or 5XX
        In case of 204 No Content, the response will be empty.

    custom_message: str, default=None
        Custom message to be returned.

    add_results: dict | list, default=None
        Add a result (dict or list) to the response. The result can be empty by
        adding an empty dict or list. If None is passed, no result will be added.
        NOTE: All this data must be JSON serializable, so the Django Model items 
        must be previously serialized (use its Serializer `Serializer(items).data`).

    add_errors: dict | list, default=None
        Add a errors (dict or list) to the response. The errors can be empty by
        adding an empty dict or list. If None is passed, no errors will be added.

    Returns
    -------
    response: rest_framework.response.Response
        The response object, the data will be serialized by the GenericResponseSerializer.
    """
    # Status code 204 means No Content
    if status_code == 204:
        return Response(status=status.HTTP_204_NO_CONTENT)
    
    if not custom_message:
        customs = {
            200: "Your request has been sent.",
            400: "Sorry, you did something wrong",
            500: "Sorry, an error has occurred.",
        }
        custom_message = customs[
            math.floor(status_code / 100) * 100
        ]  # Round a Number Down to the nearest X00

    data = {"token": token, "message": custom_message}
    if add_results:
        data["results"] = add_results
    if add_errors:
        data["errors"] = add_errors

    # Create a serializer instance with the data
    serializer = GenericResponseSerializer(data)
    
    # Return the serialized data in the Response
    return Response(serializer.data, status=status_code)


def create_paginate_response(
    token: str | uuid.UUID,
    queryset: Iterable[object],
    request: request.Request,
    serializer_class: serializers.BaseSerializer,
    many: bool = True,
    max_items_page: int = 10
):
    """ Paginate response

    Parameters
    ----------
    token: string
        UUID.hex 

    queryset: django.db.models.QuerySet
        QuerySet of items to paginate.

    request: rest_framework.request.Request
        Request object.

    serializer_class: rest_framework.serializers.Serializer
        Serializer class to use to serialize the queryset.

    many: boolean, default=True
        Determines whether the input data is a single object or a list of objects.
        - When set to `True`, the serializer will handle the input as a list of 
            objects. This is useful for lists of complex data types (e.g., objects 
            represented as dictionaries that need to be serialized individually).
        - When set to `False`, it will handle a single object. For lists of simple
            data types (e.g., strings), use `many=False` to avoid unexpected behavior 
            such as splitting strings into individual characters.

    max_items_page: int, default=10
        Max number of items to be returned by each page.
    """
    # Finally, the response is sent with pagination
    paginator = BasePaginator(
        page_size=max_items_page, 
        is_authenticated=request.user.is_authenticated
    )
    page = paginator.paginate_queryset(queryset, request)
    serializer = serializer_class(page, many=many)

    return paginator.get_paginated_response(data=serializer.data, token=token)
