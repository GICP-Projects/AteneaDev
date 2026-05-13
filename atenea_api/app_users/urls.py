"""
  This file defines the URL patterns related to user operations and authentication.

  It includes custom routes for handling social authentication.

  Both social authentication (via `social_django`) and OAuth2 token management (via 
  `drf_social_oauth2`) are configured here, providing endpoints for login, token 
  conversion, and token revocation. 
  (Replacing the default drf_social_oauth2.urls for custom URL paths.)
"""
from django.urls import re_path, include
from app_users.routers import users_auth_router
from oauth2_provider.views import AuthorizationView
from drf_social_oauth2.views import (
    ConvertTokenView,
    TokenView,
    RevokeTokenView,
    InvalidateSessions,
    DisconnectBackendView,
    InvalidateRefreshTokens,
)

app_name = 'app_users' 

urlpatterns = [

    # User endpoints related to authentication (e.g: register)
    re_path("user/", include(users_auth_router.urls)),

    # Includes social authentication routes (login, callback, disconnect) under the /auth/ prefix.
    # Handles redirection to providers (e.g., Google) and the authentication callback.
    # Example: /auth/login/google/ redirects to Google and /auth/complete/google/ handles the response.
    re_path('', include('social_django.urls', namespace='social')),

    # Includes OAuth2 token management
    re_path(r'^authorize/?$', AuthorizationView.as_view(), name='authorize'),
    re_path(r'^token/?$', TokenView.as_view(), name='token'),
    re_path(r'^token/convert/?$', ConvertTokenView.as_view(), name='convert_token'),
    re_path(r'^token/revoke/?$', RevokeTokenView.as_view(), name='revoke_token'),
    re_path(r'^invalidate/sessions/?$', InvalidateSessions.as_view(), name='invalidate_sessions'),
    re_path(r'^invalidate/refresh-tokens/?$', InvalidateRefreshTokens.as_view(), name='invalidate_refresh_tokens',),
    re_path(r'^disconnect-backend/?$', DisconnectBackendView.as_view(), name='disconnect_backend',),
]
