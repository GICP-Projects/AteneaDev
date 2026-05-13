from rest_framework import authentication

class ApiKeyAuthentication(authentication.TokenAuthentication):
    keyword = 'ApiKey'