from django.contrib import admin
from app_base.models import Query, QueryItem

from django.contrib.admin.helpers import ACTION_CHECKBOX_NAME

# ======================================================
# =====         BASE ADMIN FUNCTIONALITY           =====
# ======================================================
class BaseLinkAdmin(admin.ModelAdmin):
    readonly_fields = ["id", "link", "unique_link"]


class BaseHashAdmin(admin.ModelAdmin):
    readonly_fields = ["id", "hash_value"]


# ======================================================
# =====         QUERY ADMIN FUNCTIONALITY           =====
# ======================================================
def Delete_all_Querys(QueryAdmin, request, queryset):

    records = Query.objects.all()
    records.delete()

    Delete_all_Querys.short_description = "Delete all Querys in the DB"


class QueryAdmin(admin.ModelAdmin):
    list_display = ["token", "url", "created_at"]
    actions = [
        Delete_all_Querys,
    ]

    def changelist_view(self, request, extra_context=None):
        if (
            "action" in request.POST
            and request.POST["action"] == "Delete_all_Querys"
        ):
            if not request.POST.getlist(ACTION_CHECKBOX_NAME):
                post = request.POST.copy()
                for u in Query.objects.all():
                    post.update({ACTION_CHECKBOX_NAME: str(u.pk)})
                request._set_post(post)
        return super(QueryAdmin, self).changelist_view(request, extra_context)


admin.site.register(Query, QueryAdmin)


def Delete_all_QueryItems(QueryItemAdmin, request, queryset):

    records = QueryItem.objects.all()
    records.delete()

    Delete_all_QueryItems.short_description = "Delete all QueryItems in the DB"


class QueryItemAdmin(admin.ModelAdmin):
    list_display = ["subtoken", "query", "score"]
    actions = [
        Delete_all_QueryItems,
    ]

    def changelist_view(self, request, extra_context=None):
        if (
            "action" in request.POST
            and request.POST["action"] == "Delete_all_QueryItems"
        ):
            if not request.POST.getlist(ACTION_CHECKBOX_NAME):
                post = request.POST.copy()
                for u in QueryItem.objects.all():
                    post.update({ACTION_CHECKBOX_NAME: str(u.pk)})
                request._set_post(post)
        return super(QueryItemAdmin, self).changelist_view(
            request, extra_context
        )


admin.site.register(QueryItem, QueryItemAdmin)
