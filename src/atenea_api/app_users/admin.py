from django.contrib import admin

from .models import User
from .forms import GroupAdminForm
from django.contrib.auth.models import Group
from django.contrib.auth.admin import UserAdmin as BaseUserAdmin
from django.utils.translation import gettext_lazy as _


class UserAdmin(BaseUserAdmin):
    """customize user page in admin"""

    add_fieldsets = (
        (
            _("Personal information"),
            {
                "fields": (
                    "email",
                    "username",
                    "first_name",
                    "last_name",
                    "password1",
                    "password2",
                )
            },
        ),
        (
            _("Permissions"), 
            {"fields": ("is_superuser", "is_staff")}
        ),
    )
    fieldsets = (
        (
            _("Personal information"),
            {"fields": ("id", "email", "username", "first_name", "last_name", "password", "created_at")},
        ),
        (
            _("Permissions"), 
            {"fields": ("is_superuser", "is_staff")}
        ),
    )
    list_display = [
        "email",
        "username",
        "created_at",
    ]
    readonly_fields = ["id", "created_at"]
    search_fields = ("email", "username",)
    ordering = ("email", "username",)

    def get_inline_instances(self, request, obj=None):
        if not obj:
            return list()
        return super(UserAdmin, self).get_inline_instances(request, obj)


# register models
admin.site.register(User, UserAdmin)

# Unregister the original Group admin.
admin.site.unregister(Group)


# Create a new Group admin.
class GroupAdmin(admin.ModelAdmin):
    # Use our custom form.
    form = GroupAdminForm
    # Filter permissions horizontal as well.
    filter_horizontal = ["permissions"]


# Register the new Group ModelAdmin.
admin.site.register(Group, GroupAdmin)
