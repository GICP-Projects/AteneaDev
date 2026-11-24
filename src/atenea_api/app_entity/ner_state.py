"""Small, dependency-free helpers shared by NER ingestion and scheduling."""

from __future__ import annotations

import hashlib
import json


# Increment when the canonical text or language normalization used as NER
# input changes. Keep this synchronized with Telegram's message normalization
# code; changing it changes the input hash and makes prior results eligible for
# a later NER pass.
NER_INPUT_NORMALIZER_VERSION = "v1"


def build_ner_input_hash(
    annotated_text: str | None,
    language: str | None,
    normalizer_version: str,
) -> str:
    """Return the stable NER input identity for a MessageItem.

    JSON instead of string concatenation makes delimiters in the user text
    unambiguous and keeps the representation stable across callers.
    """

    payload = {
        "annotated_text": annotated_text or "",
        "language": language or "",
        "normalizer_version": normalizer_version,
    }
    return hashlib.sha256(
        json.dumps(payload, sort_keys=True, separators=(",", ":"), ensure_ascii=False).encode("utf-8")
    ).hexdigest()
