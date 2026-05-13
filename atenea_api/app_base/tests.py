# Import the TestCase
from django.test import TestCase
from django.utils import timezone
from rest_framework import serializers
from rest_framework.request import Request
from rest_framework.test import APIRequestFactory
from rest_framework.parsers import JSONParser
from django.contrib.auth.models import AnonymousUser
from app_base.api import *
from app_base.utils import *
from app_base.views import *
from app_base.models import Query, QueryItem
from app_base.serializers import StandardDateRangeSerializer
from app_telegram.models import SeedItem, RoomItem, UserItem    
from datetime import datetime
from urllib.parse import urlencode
import uuid 
import json

class UtilsTest(TestCase):

    def test_hash_json(self):
        # Test the hash_json function with various inputs
        test_json = {"key1": "value1", "key2": "value2"}
        expected_hash = hashlib.sha256(json.dumps(test_json).encode("utf-8")).hexdigest()
        self.assertEqual(hash_json(test_json), expected_hash)

        # Test with excluding keys
        excluding_keys = ["key1"]
        modified_json = {"key2": "value2"}  # key1 is excluded
        expected_hash_with_exclusion = hashlib.sha256(json.dumps(modified_json).encode("utf-8")).hexdigest()
        self.assertEqual(hash_json(test_json, excluding_keys=excluding_keys), expected_hash_with_exclusion)

    def test_clean_link(self):
        # Test the clean_link function with different link formats
        test_link = "https://eXamPle.com/path?query=123#section"
        self.assertEqual(clean_link(test_link), "https://example.com/path")

        # Test without removing query params
        self.assertEqual(clean_link(test_link, remove_query_params=False), "https://example.com/path?query=123")

    def test_link_to_unique(self):
        # Test the link_to_unique function with various links
        test_link = "https://example.com/path?query=123"
        self.assertEqual(link_to_unique(test_link), "example.com_path")

        # Test with already cleaned link
        cleaned_link = "https://example.com/path"
        self.assertEqual(link_to_unique(cleaned_link, already_clean=True), "example.com_path")

    def test_convert_to_standard_text(self):
        # Test convert_to_standard_text with formatted text
        formatted_text = "This is a test with ë, ü, and ö characters."
        self.assertEqual(convert_to_standard_text(formatted_text), formatted_text)  # Assuming function keeps them unchanged

    def test_markdown_to_text(self):
        # Test markdown_to_text with a simple markdown string
        markdown_string = "# Title\n**This** is a [link](https://example.com)"
        expected_output = "Title\nThis is a link: https://example.com"
        self.assertEqual(markdown_to_text(markdown_string).strip(), expected_output)

    def test_detect_lang(self):
        # Test detect_lang with a known language string
        test_string = "This is an English text."
        expected_lang = "en"  # Assuming English is detected correctly
        self.assertEqual(detect_lang(test_string), expected_lang)

    def test_parse_str_to_datetime(self):
        # Test parse_str_to_datetime with a known date string
        date_string = "2020-01-01T12:00:00"
        expected_date = timezone.make_aware(dateutil.parser.parse(date_string))
        self.assertEqual(parse_str_to_datetime(date_string), expected_date)


class ApiFunctionsTest(TestCase):

    def setUp(self):
        # Mock data
        self.item_json = {
            'link': 'https://t.me/treasurebets', 
            'title': 'TREASUREBETS💰', 
            'tags': ["Gambling"], 
            'type': 'GROUP', 
            'lang': 'en', 
        }  

        # Mock Query token
        self.token = Query.objects.create().token

    def test_save_item_json(self):
        # save_item_json is currently unused (there is no model with a 'hash_value' field)
        pass

    def test_create_item(self):
        # Call function
        created_item = create_item(self.item_json, self.token, SeedItem)
        qs = QueryItem.objects.filter(
            query__pk=self.token, object_id=created_item.pk
        ).values_list('query__pk', 'object_id')

        # Check if the items have been created correctly
        self.assertQuerysetEqual(
            qs, [(self.token, created_item.pk)], ordered=False
        )

    def test_update_item(self):
        # Call function
        created_item = create_item(self.item_json, self.token, SeedItem)
        update_item(created_item, self.token) # This will create another 

        # Check if the Query items have been created correctly
        self.assertEqual(created_item.query_items.count(), 2)

    def test_bulk_add_generic_relationships(self):
        room_item = {'tg_id': 1000000000, 'link': f'https://t.me/canal1'}
        users = [
            {
                'pk': uuid.uuid4(), 
                'tg_id': int(f"{i}000000000"), 
            }
            for i in range(1,11)
        ]
        created_room = RoomItem.objects.create(**room_item)
        created_users = UserItem.objects.bulk_create([UserItem(**u) for u in users])

        bulk_add_generic_relationships(
            UserItem,
            'rooms',
            RoomItem,
            {created_room.pk: [us.pk for us in created_users]},
        )
        self.assertEqual(created_room.members.count(), 10)

    def test_bulk_add_query_relationships(self):
        users = [
            {
                'pk': uuid.uuid4(), 
                'tg_id': int(f"{i}000000000"), 
            }
            for i in range(1,11)
        ]
        created_users = UserItem.objects.bulk_create([UserItem(**u) for u in users])

        bulk_add_query_relationships(created_users, self.token)


class ViewsTest(TestCase):

    class TestSerializer(StandardDateRangeSerializer):
        # List field: text=...&text=...text=...
        text = serializers.ListField(
            child=serializers.CharField(max_length=64, allow_blank=False),
            required=False, 
            max_length=20 
        )

    def setUp(self):
        self.factory = APIRequestFactory()
        # POST
        self.post_temp_request = self.factory.post(
            "/v1/api/EXAMPLE1", 
            data=json.dumps({'date-start': '01/07/2022', 'date-end': '11/05/2023'}),
            content_type='application/json'
        )
        self.post_rest_request = Request(self.post_temp_request, parsers=[JSONParser()])
        # GET 
        params = urlencode(
            {   
                # List field: text=...&text=...text=...
                "text": ["texto1", "texto2", "texto3"]
            },
            True # To force format -> text=...&text=...text=...
        )
        self.get_temp_request = self.factory.get(
            f"/v1/api/EXAMPLE1?{params}", 
        )
        self.get_rest_request = Request(self.get_temp_request)

    def test_parse_request_data(self):

        # POST
        data = parse_request_data(self.post_rest_request, self.TestSerializer)
        test_start_date = timezone.make_aware(datetime(2022, 7, 1))
        test_end_date = timezone.make_aware(datetime(2023, 5, 11))
        self.assertEqual(
            test_start_date.strftime("%m/%d/%Y"), data["date_start"].strftime("%m/%d/%Y")
        )
        self.assertEqual(
            test_end_date.strftime("%m/%d/%Y"), data["date_end"].strftime("%m/%d/%Y")
        )

        # GET
        params = parse_request_data(
            self.get_rest_request, 
            self.TestSerializer,
            list_params=["text"] # Select params that need to be a list
        )
        self.assertEqual(type(params['text']), list)
        self.assertEqual(len(params['text']), 3)

    def test_log_query(self):
        token = log_query(self.post_rest_request, {'key': 'value'})
        # Test the created Query
        query = Query.objects.get(pk=token)
        self.assertEqual(query.url, "EXAMPLE1")
        self.assertEqual(query.method, "POST")
        self.assertEqual(query.location, "localhost")
        self.assertEqual(query.owner, None)
        self.assertEqual(query.data, {'key': 'value'})

    def test_create_message(self):
        token = uuid.uuid4()
        response = create_message(token, status_code=404)
        self.assertEqual(token, response.data["token"])
        self.assertEqual("Sorry, you did something wrong", response.data["message"])
