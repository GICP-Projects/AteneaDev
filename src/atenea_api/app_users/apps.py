from django.apps import AppConfig
from drf_social_oauth2 import UNICODE_ASCII_CHARACTER_SET
try:
    from secrets import SystemRandom
except ImportError:
    from random import SystemRandom


def generate_token(request, length=30, chars=UNICODE_ASCII_CHARACTER_SET):
    """Generates a non-guessable OAuth Json Web Token
    OAuth (1 and 2) does not specify the format of tokens except that they
    should be strings of random characters. Tokens should not be guessable
    and entropy when generating the random characters is important. Which is
    why SystemRandom is used instead of the default random.choice method.

    JWT Token content:
        - token
        - name
        - email
    """
    from django.conf import settings
    import jwt
    #from jose import jwt

    rand = SystemRandom()
    # Use JWT signing secret instead of Django secret
    secret = getattr(settings, 'JWT_SIGNATURE_SECRET')

    token = ''.join(rand.choice(chars) for x in range(length))
    jwtted_token = jwt.encode(
        {   
            # Add name and email into the payload instead of only the token
            'token': token, 
            'name': f"{request.user.first_name}" + (f" {request.user.last_name}" if request.user.last_name else ""), 
            'email': request.user.email
        }, 
        secret, algorithm='HS256'
    )
    return jwtted_token


class UsersConfig(AppConfig):
    default_auto_field = 'django.db.models.BigAutoField'
    name = "app_users"