import hashlib
import mimetypes
import re
from dataclasses import dataclass
from pathlib import Path
from urllib.parse import urlparse

import boto3
from botocore.client import Config
from botocore.exceptions import ClientError
from django.conf import settings


EXECUTABLE_EXTENSIONS = {
    ".exe",
    ".msi",
    ".bat",
    ".cmd",
    ".ps1",
    ".vbs",
    ".js",
    ".scr",
    ".dll",
    ".com",
    ".jar",
}


@dataclass(frozen=True)
class MediaRisk:
    level: str
    reason: str
    potentially_dangerous: bool


def normalize_extension(extension: str) -> str:
    extension = (extension or "").strip().lower()
    if extension and not extension.startswith("."):
        extension = f".{extension}"
    return extension


def classify_media_risk(extension: str, mime_type: str = "") -> MediaRisk:
    extension = normalize_extension(extension)
    mime_type = (mime_type or "").lower()
    if extension in EXECUTABLE_EXTENSIONS:
        return MediaRisk("high", "executable_extension", True)
    if "executable" in mime_type or "x-msdownload" in mime_type:
        return MediaRisk("high", "executable_mime_type", True)
    return MediaRisk("low", "", False)


def guess_extension(file_name: str = "", mime_type: str = "") -> str:
    suffix = Path(file_name or "").suffix.lower()
    if suffix:
        return suffix
    guessed = mimetypes.guess_extension(mime_type or "")
    return normalize_extension(guessed or "")


def sha256_bytes(data: bytes) -> str:
    return hashlib.sha256(data).hexdigest()


def safe_object_part(value: str, default: str = "item") -> str:
    cleaned = re.sub(r"[^a-zA-Z0-9._-]+", "_", (value or "").strip())
    return cleaned.strip("._") or default


def build_object_key(room_name: str, message_id: int, file_name: str, sha256: str) -> str:
    safe_room = safe_object_part(room_name, "room")
    safe_file = safe_object_part(file_name, f"message_{message_id}")
    return f"telegram-media/{safe_room}/{message_id}/{sha256[:16]}_{safe_file}"


def get_s3_client():
    config = settings.MEDIA_S3
    return boto3.client(
        "s3",
        endpoint_url=config["endpoint_url"],
        aws_access_key_id=config["access_key"],
        aws_secret_access_key=config["secret_key"],
        region_name=config["region"],
        use_ssl=config["use_ssl"],
        verify=config["verify_ssl"],
        config=Config(s3={"addressing_style": config["addressing_style"]}),
    )


def ensure_bucket_exists() -> None:
    if not settings.MEDIA_S3["create_bucket"]:
        return
    client = get_s3_client()
    bucket = settings.MEDIA_S3["bucket"]
    try:
        client.head_bucket(Bucket=bucket)
    except ClientError:
        client.create_bucket(Bucket=bucket)


def upload_bytes(object_key: str, data: bytes, content_type: str = "") -> None:
    ensure_bucket_exists()
    extra_args = {"ContentDisposition": "attachment"}
    if content_type:
        extra_args["ContentType"] = content_type
    get_s3_client().put_object(
        Bucket=settings.MEDIA_S3["bucket"],
        Key=object_key,
        Body=data,
        **extra_args,
    )


def delete_object(object_key: str) -> None:
    if not object_key:
        return
    get_s3_client().delete_object(
        Bucket=settings.MEDIA_S3["bucket"],
        Key=object_key,
    )


def generate_download_url(object_key: str, file_name: str = "") -> str:
    params = {
        "Bucket": settings.MEDIA_S3["bucket"],
        "Key": object_key,
        "ResponseContentDisposition": f'attachment; filename="{safe_object_part(file_name, "download")}"',
    }
    return get_s3_client().generate_presigned_url(
        "get_object",
        Params=params,
        ExpiresIn=settings.MEDIA_S3["presigned_ttl_seconds"],
    )


def normalize_domain(url: str) -> str:
    return (urlparse(url).netloc or "").lower().split("@")[-1].split(":")[0]


def provider_from_domain(domain: str) -> str:
    domain = (domain or "").lower()
    mappings = {
        "google": ["drive.google.com", "docs.google.com", "storage.googleapis.com"],
        "dropbox": ["dropbox.com", "dl.dropboxusercontent.com"],
        "onedrive": ["onedrive.live.com", "1drv.ms", "sharepoint.com"],
        "mega": ["mega.nz", "mega.co.nz"],
        "mediafire": ["mediafire.com"],
        "wetransfer": ["wetransfer.com", "we.tl"],
        "box": ["box.com", "app.box.com"],
        "icloud": ["icloud.com"],
        "pcloud": ["pcloud.com", "my.pcloud.com"],
        "sync": ["sync.com"],
        "icedrive": ["icedrive.net"],
        "terabox": ["terabox.com"],
        "github": ["github.com", "raw.githubusercontent.com"],
        "gitlab": ["gitlab.com"],
        "bitbucket": ["bitbucket.org"],
        "archive": ["archive.org"],
        "ipfs": ["ipfs.io", "cloudflare-ipfs.com"],
    }
    for provider, domains in mappings.items():
        if domain in domains or any(domain.endswith(f".{item}") for item in domains):
            return provider
    if "nextcloud" in domain:
        return "nextcloud"
    if "owncloud" in domain:
        return "owncloud"
    return domain.split(".")[-2] if "." in domain else domain


def is_whitelisted_download_domain(domain: str) -> bool:
    domain = (domain or "").lower()
    for pattern in settings.MEDIA_EXTERNAL_URL_WHITELIST:
        pattern = pattern.lower()
        if pattern.endswith(".*"):
            if pattern[:-2] in domain:
                return True
        elif domain == pattern or domain.endswith(f".{pattern}"):
            return True
    return False
