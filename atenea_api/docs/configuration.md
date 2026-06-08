
## Telegram API access
To use any endpoint that extracts Telegram data by its API it's necessary to add these credentials.

Go to `http://0.0.0.0:8000/admin/app_telegram/telegramauth/` and create a new Telegram Auth item. Is necessary to add the `api_id`, `api_hash`, and a `session token`.

To generate a session token run the following code:
```python
from telethon.sync import TelegramClient
from telethon.sessions import StringSession

# Generating a new one
async with TelegramClient(StringSession(), api_id, api_hash) as client:
    print(StringSession.save(client.session))
```

Also, you can store the token inside a file instead of printing it on the screen:
```python
async with TelegramClient(StringSession(), api_id, api_hash) as client:
    with open("name.session", "w") as file:
        file.write(StringSession.save(client.session))
```

**NOTE**: The platform will use these Telegram credentials in the ETL data pipeline (when data for Channels/Users/Messages is extracted). By default, it will parallelize the workload depending on the number of credentials available. However, we can force more parallelization by using the `block_size` parameter (**Warning**: This may result in temporary bans).


## API KEY configuration
To use the API endpoints with an api key token it is necessary to create a Token
in the Admin panel. Add this token in any required endpoint by adding the following
header:

`'Authorization': 'ApiKey ....'`


## OAuth2 configuration #TODO

1.- Go to the Admin panel.
2.- Django oauth toolkit app > Application
3.- Create a new application (https://github.com/wagnerdelima/drf-social-oauth2). Save the client_id and client_secret
4.- Go to the Google OAuth panel (https://console.developers.google.com/apis/credentials) and create a new project (or use an existing one)
5.- Create there a new "Oauth Client ID". Once it's created, Google will show you a Client ID and a Client Secret. Insert these credentials in the environment variables (GOOGLE_OAUTH2_KEY, GOOGLE_OAUTH2_SECRET)
