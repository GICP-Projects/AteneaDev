from rest_framework.routers import SimpleRouter
from app_metadata.views import CategoryView


metadata_router = SimpleRouter(trailing_slash=False)

# `Categories` and `Sentiments` endpoints.
metadata_router.register(r'category', CategoryView, basename='category-staff')