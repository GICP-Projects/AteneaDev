"""NER ledger states and task outcomes, preserving existing stored values."""

from enum import StrEnum

from django.db.models import IntegerChoices, TextChoices


class EntityType(IntegerChoices):
    """Stable numeric codes for entity types stored in the database."""

    ORG = 1, "Companies, agencies, institutions."
    GPE = 2, "Geopolitical entity, i.e. countries, cities, states."
    PER = 3, "People, including fictional."
    EVENT = 4, "Named hurricanes, battles, wars, sports events, etc."
    PRODUCT = 5, "Objects, vehicles, foods, etc. (Not services.)"
    NORP = 6, "Nationalities or religious or political groups."
    LAW = 7, "Named documents made into laws."
    LANGUAGE = 8, "Any named language."
    LOCATION = 9, "Non-GPE locations, mountain ranges, bodies of water."
    MONEY = 10, "Monetary values, including unit."
    DATE = 11, "Absolute or relative dates or periods."
    MISC = 12, "Miscellaneous named entities."
    EMAIL = 13, "Any type of email."
    URL = 14, "Any type of URL."
    MENTION = 15, "Standard social network mentions (@name)."
    HASHTAG = 16, "Standard social network hashtags (#hashtag)."
    EMOJI = 17, "Any type of emoji."
    WALLET_ETH = 18, "Ethereum (ETH) wallet address."
    WALLET_BTC = 19, "Bitcoin (BTC) wallet address."
    WALLET_DASH = 20, "Dash (DASH) wallet address."
    WALLET_XMR = 21, "Monero (XMR) wallet address."

    @classmethod
    def from_name(cls, name):
        """Return an entity type code from its textual name or numeric code."""

        if isinstance(name, cls):
            return name.value
        if isinstance(name, int):
            return cls(name).value
        try:
            normalized = str(name).strip().upper()
            try:
                return cls[normalized].value
            except KeyError:
                return cls(int(normalized)).value
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unknown entity type: {name!r}") from exc

    @classmethod
    def to_name(cls, code):
        """Return the textual name for an entity type code or name."""

        if isinstance(code, str) and not code.strip().isdigit():
            try:
                return cls[code.strip().upper()].name
            except KeyError as exc:
                raise ValueError(f"Unknown entity type: {code!r}") from exc
        try:
            return cls(int(code)).name
        except (TypeError, ValueError) as exc:
            raise ValueError(f"Unknown entity type: {code!r}") from exc


from_name = EntityType.from_name
to_name = EntityType.to_name


class NERStatus(TextChoices):
    CLAIMED = "claimed", "Claimed"
    EXTRACTING = "extracting", "Extracting"
    EXTRACTED = "extracted", "Extracted"
    STORING = "storing", "Storing"
    STORED = "stored", "Stored"
    COMPLETED = "completed", "Completed"
    RETRY_WAIT = "retry_wait", "Waiting to retry"
    FAILED = "failed", "Failed"


class NERTaskOutcome(StrEnum):
    EXTRACTED = "extracted"
    STORED = "stored"
