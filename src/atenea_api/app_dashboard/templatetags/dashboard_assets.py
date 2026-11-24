from pathlib import Path

from django import template
from django.contrib.staticfiles import finders
from django.templatetags.static import static


register = template.Library()


@register.simple_tag
def dashboard_static(path):
    """Refresh cached dashboard assets whenever their source file changes."""
    url = static(path)
    local_path = finders.find(path)
    if not local_path:
        return url
    try:
        version = Path(local_path).stat().st_mtime_ns
    except OSError:
        return url
    separator = "&" if "?" in url else "?"
    return f"{url}{separator}v={version:x}"
