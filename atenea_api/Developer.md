# DEVELOPER GUIDE V0.0.1

## HOW TO STRUCTURE THE PROJECT

#### ENDPOINT PIPELINE:
```
HTTP REQUEST -> ROUTER --> VIEWS.PY --> SERIALIZER (VALIDATE AND CLEAN PARAMS) -->
API.PY (THE PARAMS RECEIVED ARE ALREADY CLEAN AND CORRECT)
```

#TODO Write this section
- **app_base** contains the global functionality that can be used by any other app.
  - **views.py**: contains functions for any views.py of any app
  - **models.py**: contains funcions for any models.py of any app, global Models and global Models templates.
  - **serializers.py**: contains global Serializers and global Serializers templates.
  - **utils.py**: contains general functionality for the whole project.
  - **api.py**: contains global functions focused on data storage and the creation of relationships between models.

- app_\<name> contains all its functionality 
  - **views.py**: Contains each endpoint's function that is going to be called once the HTTP request is send. 
  - **routers.py** Binds each of the above functions from views.py to an especific URL path.
  - **models.py** Contains all the required models for app_\<name>. In general, any project model should use the app_base's BaseModel because it adds a UUID field as primary key which gives much more flexibility to the databse structure, and also, adds new method 'bulk_create_or_update()' (very useful). 
  - **serializers.py** Contains two types of serializers:
    - Input serializers: To validate the parameters sent in any requests, each endpoint will have its own input serializer for its specific parameters. **REMEMBER**: The input serializers should validate (`validate_<field>()`) and clean/prepare (`to_internal_value()`) the parameters.
    - Ouput serializers: ModelSerializers to choose how many information from each model share in each endpoint'sresponse. 
    **Remember**: You can prepare/clean the data to be returned in the serializers by overriding the `to_representation()` method.
  - **services**: A folder with all the main functionality of this app_*. It's only required to have a api.py file with the core functions of each endpoint (each view.py function will be bound with an api.py function). It's free to have more files to structure the code as you desire. All the logic related to the write/read of the database goes here (**REMEMBER**: Use the app_base's api.py functions this purpose).
  - **utils.py**: Contains general functionality for this app_* (f.e: telegram_link_normalizer to unify the links of telegram in app_telegram.)

**NOTE**: views.py from each app will contain two types of Views:
    - StaffView: Related with the admin endpoints (Is mandatory log-in with an admin account).
    - ClientView: Related with the client endpoints that don't require log-in or admin account.


## HOW TO CREATE SERIALIZERS

In this framework, serializers have multiple roles: they can receive a request in JSON, URL-encoded, etc., validate its fields, and produce a dictionary. Conversely, they can also create JSON from data to send back to the client.

To maintain consistency across the project, serializers should adhere to the following guidelines:

### Serializers for Validating Requests

1. **Header Comment Indication**:
    - Clearly indicate that the serializer is for incoming requests by adding `[REQUEST]` in the header comment.
    
    ```python
    # ==================================================================
    # 01.1 - [REQUEST] Filter Category Serializers
    # ==================================================================
    ```

2. **Field Definitions**:
    - Include all fields with their constraints (maximum value, minimum value, default value, desired format, etc.).
    
    ```python
    class ExampleRequestSerializer(serializers.Serializer):
        username = serializers.CharField(max_length=100, help_text="The username of the user.")
        age = serializers.IntegerField(min_value=0, max_value=120, help_text="The age of the user.")
        email = serializers.EmailField(help_text="The email address of the user.")
    ```

3. **Cleaned and Validated Data**:
    - Use the `validate_<field>` method to perform any additional validation and clean the relevant fields. For example, if URLs are received and only the clean URL without parameters is needed, the `validate` method should clean it.
    
    ```python
    class ExampleRequestSerializer(serializers.Serializer):
        url = serializers.URLField(help_text="The URL to be processed.")
        
        def validate_url(self, value):
            # Clean the URL by removing query parameters
            parsed_url = urlparse(value)
            clean_url = parsed_url.scheme + "://" + parsed_url.netloc + parsed_url.path
            return clean_url
    ```
    - Ensure that the functions in `api.py` which receive the data from the serializers get it fully validated and cleaned. This means all data processing, such as removing unwanted URL parameters, must be handled within the serializer.

4. **Field Descriptions**:
    - Add `help_text` to each field describing its purpose. This is used by the `drf-spectacular` library to generate documentation.
    
    ```python
    class ExampleRequestSerializer(serializers.Serializer):
        username = serializers.CharField(max_length=100, help_text="The username of the user.")
        password = serializers.CharField(max_length=100, help_text="The password for the user.")
    ```

### Example

```python
# ==================================================================
# 01.1 - [REQUEST] Create User Serializer
# ==================================================================

class UserRequestSerializer(serializers.Serializer):
    username = serializers.CharField(
        max_length=150,
        help_text="The unique username for the user. Maximum length is 150 characters."
    )
    email = serializers.EmailField(
        help_text="The email address of the user. Must be a valid email format."
    )
    age = serializers.IntegerField(
        min_value=0,
        max_value=120,
        help_text="The age of the user. Must be between 0 and 120."
    )
    website = serializers.URLField(
        required=False,
        help_text="Optional. The personal website URL of the user."
    )
    
    def validate_website(self, value):
        # Clean the URL by removing query parameters
        parsed_url = urlparse(value)
        clean_url = parsed_url.scheme + "://" + parsed_url.netloc + parsed_url.path
        return clean_url
```


## HOW TO DOCUMENT THE CODE

### Functions/Methods
Any function or method must be documented using the following format:

```Python
def function(
    param1,
    param2=[],
    param3=None,
):
    """<One-Two lines of a short description describing the general functionality>

    <Any block of text with more detailed information like specific functionality>

    Parameters
    ----------
    param1: <type> 
        <Param description>

    param2: list[type], default=[]
        <Param description>

    param3: <type>, default=None
        <Param description>

    Returns
    -------
    ret: <type>
        <Return variable description>
    """
    pass
```

The type of each param or return variable must be clearly specified. It can be of any standard Python type: **list, dict, int, float, string, etc...**, but also from any library by specifing the full path, f.e: 
```
    Parameters
    ----------
    param1: rest_framework.serializers.Serializer
        <Param description>
```

If the parameter instead of being an instance of a class contains the class itself is necessary to modify the format:
```
    Parameters
    ----------
    param1: class (<type>)
        <Param description>
```