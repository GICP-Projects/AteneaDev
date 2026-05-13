import uuid
import json
import datetime
from django.conf import settings
from django.db import models, transaction
from django.core.validators import URLValidator
from django.utils import timezone
from django.utils.translation import gettext_lazy as _
from django.contrib.contenttypes.fields import GenericForeignKey
from django.contrib.contenttypes.models import ContentType
from django.db.models.fields import CharField
from app_users.models import User


class GeneralEncoder(json.JSONEncoder):
    """ An optional json.JSONEncoder subclass to serialize data types not supported 
    by the standard JSON serializer (e.g. datetime.datetime or UUID). For example, 
    you can use the DjangoJSONEncoder class.
    Help: https://stackoverflow.com/a/12126976

    It will encode datetimes in ISO Format and UUIDs in hexadecimal string.

    With this Encoder, Django JSONField will accept dicts with datetimes and UUID 
    values
    """

    def default(self, obj):
        if isinstance(obj, uuid.UUID):
            return obj.hex 
        elif isinstance(obj, (datetime.datetime, datetime.date, datetime.time)):
            return obj.isoformat()
        elif isinstance(obj, datetime.timedelta):
            return (datetime.datetime.min + obj).time().isoformat()
        return super().default(obj)


# ======================================================
# =====                 QUERY MODELS               =====
# ======================================================
class Query(models.Model):

    token = models.UUIDField(default=uuid.uuid4, primary_key=True)
    created_at = models.DateTimeField(default=timezone.now)

    location = models.CharField(max_length=255, blank=True, default="")
    owner = models.ForeignKey(
        User, related_name="query", on_delete=models.CASCADE, null=True
    )
    url = models.URLField(max_length=255, null=True, blank=True)

    HTTP_METHODS = [
        ("GET", _("GET")), 
        ("POST", _("POST")), 
        ("PUT", _("PUT")), 
        ("PATCH", _("PATCH")), 
        ("DELETE", _("DELETE"))
    ]
    method = models.CharField(choices=HTTP_METHODS)
    data = models.JSONField(default=dict, null=True, blank=True, encoder=GeneralEncoder)

    def __str__(self):
        return f"ID: {self.token}, created: {self.created_at} "


class QueryItem(models.Model):
    subtoken = models.UUIDField(default=uuid.uuid4, primary_key=True)

    # Generic relation
    content_type = models.ForeignKey(
        ContentType, on_delete=models.CASCADE, null=True
    )
    object_id = models.UUIDField(null=True)
    content_object = GenericForeignKey("content_type", "object_id")

    query = models.ForeignKey(
        Query, on_delete=models.CASCADE, related_name="query_items", null=True
    )
    score = models.FloatField(default=0)


# ======================================================
# =====      BASE MODELS AND FUNCTIONALITY         =====
# ======================================================
class BaseModel(models.Model):
    """Main Django model to use in this project which contains all the necessary
    functionality (id uuid primary key, extra features).
    """

    # 'Universal' unique identifier non-related with the object data
    id = models.UUIDField(primary_key=True, default=uuid.uuid4, editable=False)

    def __str__(self):
        return f"uuid: {self.id}"

    class Meta:
        abstract = True

    @classmethod
    def bulk_create_or_update(cls, items, unique_key_name, update_fields = []):
        """Bulk function to add a secure method to update or create a bunch of items
        at once and return all the affeced items.

        Django >= 4.1 added the functionality to updated items using the default
        .bulk_create() method. 

        So, why is it necessary to use all these instructions that retrieve existing 
        items instead of just using .bulk_create()? The problem leads in the fact
        that BaseModel uses an unique UUID field as primary key which is generated 
        any time an instance is created. Therefore, if we create an instance 
        (f.e: Model(**item)) of an exisiting item, the returned instance will have
        a random pk that doesn't correspond to the actual pk of the item. While this 
        isn't a problem for the bulk_create method (because it uses an unique_field 
        to identify existing elements), we should have the correct pks in order
        to be able to link the elements to their QueryItems (f.e), or to perform any 
        kind of subsequent operation that requires the primary key. 

        Parameters
        ----------
        cls: class 
            Reference of the class.

        items: list[dict]
            List of dictionaries with the items to create or update. The keys from
            these dicts are all the fields of the item that we want to fill or update.

        unique_key_name: string
            Unique field_name. it can't be the 'id' primary key.

        update_fields: list[str], default=[]
            List of fields are going to be updated. By default this param will be
            empty, therefore, all fields (except pks or unique ones) are going to 
            be selected.
            NOTE: If you send an existing item without a field (f.e 'collected_at')
            but update_fields = [], all fields will be updated, including 
            'collected_at', which will be changed to its default value. That's why 
            is important to choose which fields do you want to update in order to 
            avoid unexpected behaviours.

        Returns
        ----------
        affected_items: list[dict]
            Created/Updated items

        existing_items_pk: list[uuid.UUID]
            Primary keys of the updated items
        """

        with transaction.atomic():
            # First, we are going to retrieve already existing items (that are going
            # to be updated) to get its primary keys (uuid)
            exisiting_items = cls.objects.filter(
                **{f'{unique_key_name}__in': [d[unique_key_name] for d in items]}
            ).select_for_update().values(unique_key_name, 'pk') # locks the selected items

            # Create a dictionary of existing pks with unique field as the key for easy lookup
            existing_items_pk = {
                item[unique_key_name]: {'pk': item['pk']} for item in exisiting_items
            }

            # Create all items instances With the items to be updated with the correct 
            # primary key.
            items_to_create_or_update = [
                cls(**item) if (unique_field_ := item.get(unique_key_name)) not in existing_items_pk 
                else cls(**item, **existing_items_pk[unique_field_]) 
                for item in items
            ]
            
            # Create/update
            if not update_fields:
                update_fields = [
                    field.name for field in cls._meta.fields if not field.unique
                ]
            returned_items = cls.objects.bulk_create(
                items_to_create_or_update,
                update_conflicts=True,
                unique_fields=[unique_key_name],
                update_fields=update_fields,
                batch_size=settings.BULK_BATCH_SIZE
            )

            return returned_items, [item["pk"] for item in existing_items_pk.values()]


class BaseLinkItem(BaseModel):
    """
     - id (primary key fully independent from the item content).
        If a webpage change its structure we can edit our unique_link/link fields
        without affecting our database relationships structure.

    - unique_link.
        Used to identify duplicates. This link don't work to retrieve the webpage but
        helps to identify minor changes. f.e:
            unique_link = www.bbc.com_hello-to-world
            link = https://www.bbc.com/hello-to-world/
            link2 = http://www.bbc.COm/hello-to-world/
            link3 = https://www.Bbc.com/hello-to-world?param=12
        All this links retrieve the same webpage.

        Main field used to check if the item already exist.

    - link.
        First version of the link inserted. It works to retrieve the page.
        Some previous clean (lowercase and, if we want, remove queryparams,
        f.e: newspapers articles, not expections found, only use them for analytics
        purpose.)

        Although it is unique, is preferible to use unique_link when we want to
        check or search already existing items.


    Some models like SitemapItem or RSS will allow query params (and they will
    be displayed in both unique_link and link)
    """

    unique_link = models.CharField(max_length=255, unique=True)
    link = models.URLField(
        validators=[URLValidator()],
        max_length=2048,
        unique=True,
    )

    def __str__(self):
        return f"Link: {self.link}"

    class Meta:
        abstract = True


class BaseHashItem(BaseModel):

    # Unique hash built from the object data
    hash_value = models.CharField(max_length=255, unique=True)

    def __str__(self):
        return f"Hash_value: {self.hash_value}"

    class Meta:
        abstract = True


# ======================================================
# =====             ADDITIONAL FIELDS              =====
# ======================================================

class LanguageField(CharField):
    """
    A language field for Django models.

    COPYRIGHT NOTICE AND ATTRIBUTION

    This code snippet was copied and adapted from the following source:

    Repository: django-language-field
    URL: [https://github.com/audiolion/django-language-field]
    Author: audiolion
    """
    MAX_ALLOWED_LENGTH = 2 # LANG ISO-639-1

    def __init__(self, *args, **kwargs):
        # Local import so the languages aren't loaded unless they are needed.
        from app_base.languages import LANGS_ISO_639_1

        kwargs.setdefault('max_length', self.MAX_ALLOWED_LENGTH)
        kwargs.setdefault('choices', LANGS_ISO_639_1)
        kwargs.setdefault('db_collation', None)

        super().__init__(*args, **kwargs)