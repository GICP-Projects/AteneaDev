from rest_framework.routers import SimpleRouter
from app_frontend.views import FrontFormView


front_router = SimpleRouter(trailing_slash=False)

# `FrontForms` endpoints.
front_router.register(r"form", FrontFormView, basename="form")