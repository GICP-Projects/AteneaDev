from django import forms
from django.contrib import admin
from django.utils.translation import gettext_lazy as _
from phonenumber_field.formfields import PhoneNumberField, SplitPhoneNumberField
from .models import (
    MessageItem,
    RoomItem,
    SeedItem,
    TelegramAuth,
    TelegramExternalUrlItem,
    TelegramMediaItem,
    UserItem,
)


# ======================================================
# =====         SEED ADMIN FUNCTIONALITY           =====
# ======================================================
class SeedItemAdmin(admin.ModelAdmin):
    list_display = ["id", "link", "type", "is_seeded", "collected_at"]
    readonly_fields= ["link", "collected_at", "is_seeded"]

admin.site.register(SeedItem, SeedItemAdmin)


# ======================================================
# =====       AUTH TG ADMIN FUNCTIONALITY          =====
# ======================================================


class TelegramAuthForm(forms.ModelForm):
    phone = SplitPhoneNumberField(label=_("Phone number"))
    class Meta:
        model = TelegramAuth
        fields = '__all__'
        #field_classes = {'phone': SplitPhoneNumberField}
        widgets = {
            'session': forms.PasswordInput(
                render_value=False, 
                attrs={'placeholder': _("Required session.")}
            )
        }

    def save(self, commit=True):
        if self.instance.pk and not self.cleaned_data['session']:
            # If editing and session is empty, retain the original value
            self.instance.session = self.Meta.model.objects.get(pk=self.instance.pk).session
        return super().save(commit=commit)

class EditTelegramAuthForm(TelegramAuthForm):
    """Custom form to allow editing TelegramAuth items without session."""
    def __init__(self, *args, **kwargs):
        super().__init__(*args, **kwargs)
        # Edit placeholder for session
        self.fields['session'].widget.attrs['placeholder'] = _("Leave empty to keep original.")
        # Edit session required attribute
        if self.instance and self.instance.pk:
            self.fields['session'].required = False

class TelegramAuthAdmin(admin.ModelAdmin):
    form = TelegramAuthForm
    list_display = ["id", "name", "phone", "is_valid"]
    fieldsets = (
        (None, {
            'fields': ('name', 'phone')
        }),
        (_('Telegram API credentials'), {
            'fields': ('api_id', 'api_hash', 'session')
        }),
        (_('Metadata'), {
            'fields': ('is_valid', 'is_hibernated', 'counter', 'wait_until')
        }),
    )

    def get_readonly_fields(self, request, obj=None):
        if obj is None:
            return ("counter", "wait_until")
        return ("counter", "wait_until")
    
    def add_view(self, request, form_url="", extra_context=None):
        # In case of adding a new item, use the TelegramAuthForm (session is required)
        self.form = TelegramAuthForm
        return super().add_view(request, form_url, extra_context)

    def change_view(self, request, object_id, form_url="", extra_context=None):
        # In case of editing an item, use the EditTelegramAuthForm (to allow not required session)
        self.form = EditTelegramAuthForm
        return super().change_view(request, object_id, form_url, extra_context)

admin.site.register(TelegramAuth, TelegramAuthAdmin)


################################################
class BaseTelegramAdmin(admin.ModelAdmin):
    """ Base Admin model to customize any action

    Overrding delete methods:
    In case a RoomItem/UserItem is deleted in the admin panel the related
    SeedItem is going to update its "is_seeeded" field to False
    """
    def delete_queryset(self, request, queryset):
        seed_items_pks = queryset.values_list("seed_item__pk", flat=True)
        SeedItem.objects.filter(pk__in=seed_items_pks).update(is_seeded=False)
        queryset.delete()

    def delete_model(self, request, obj):
        obj.seed_item.is_seeded = False
        obj.seed_item.save()
        obj.delete()

    class Meta:
        abstract = True

# ======================================================
# =====         ROOM ADMIN FUNCTIONALITY           =====
# ======================================================
class RoomItemAdmin(BaseTelegramAdmin):
    list_display = ["id", "link", "is_channel", "lang", "created_at", "last_update"]
    readonly_fields= ["tg_id", "link", "unique_name"]

admin.site.register(RoomItem, RoomItemAdmin)


# ======================================================
# =====         USER ADMIN FUNCTIONALITY           =====
# ======================================================
class UserItemAdmin(BaseTelegramAdmin):
    list_display = ["id", "unique_name", "is_bot", "lang"]
    readonly_fields= ["tg_id", "unique_name"]

admin.site.register(UserItem, UserItemAdmin)

#############################################


# ======================================================
# =====        MESSAGE ADMIN FUNCTIONALITY         =====
# ======================================================
class MessageItemAdmin(admin.ModelAdmin):
    list_display = ["id", "room", "msg_id", "get_text_length", "media_type", "lang"]
    readonly_fields= ["room", "msg_id", "sender", "media_type"]

    def get_text_length(self, obj):
        # Calculate the length of the "text" field
        return len(obj.text)

admin.site.register(MessageItem, MessageItemAdmin)


class TelegramMediaItemAdmin(admin.ModelAdmin):
    list_display = ["id", "room", "message", "status", "extension", "risk_level", "downloaded_at"]
    readonly_fields = ["message", "room", "bucket", "object_key", "sha256", "downloaded_at"]
    search_fields = ["original_file_name", "sha256", "message__link", "room__unique_name"]
    list_filter = ["status", "risk_level", "extension"]


admin.site.register(TelegramMediaItem, TelegramMediaItemAdmin)


class TelegramExternalUrlItemAdmin(admin.ModelAdmin):
    list_display = ["id", "room", "message", "provider", "domain", "status", "last_seen_at"]
    readonly_fields = ["message", "room", "url", "domain", "provider", "detected_at", "last_seen_at"]
    search_fields = ["url", "domain", "provider", "message__link", "room__unique_name"]
    list_filter = ["provider", "status", "domain"]


admin.site.register(TelegramExternalUrlItem, TelegramExternalUrlItemAdmin)
