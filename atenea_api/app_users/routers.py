from rest_framework.routers import SimpleRouter
from app_users.views import UserAuthView

users_auth_router = SimpleRouter(trailing_slash=False)

# `user` endpoints.
users_auth_router.register(r"", UserAuthView, basename="user-auth")

#users_router.register(r"profile", ProfileView, basename="profile")