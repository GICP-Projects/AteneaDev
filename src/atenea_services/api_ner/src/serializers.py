from settings import ALLOWED_LANG_MODELS, MAX_DATA_BY_REQUEST
from pydantic import BaseModel, Field, field_validator
from typing import List, Optional


# ======================================================
# =====            REQUEST SERIALIZER              =====
# ======================================================

class TextItem(BaseModel):
    # Field to add the same id in the returned response
    id: Optional[str] = Field(
        None, 
        description=(
            "Identifier associated with the text, enabling traceability between"
            " the input text and the resulting response."
        )
    ) 
    text: str = Field(..., min_length=1)

    @field_validator("text")
    @classmethod
    def check_text_has_content(cls, value):
        if not value.strip():
            raise ValueError("Text must contain at least one non-whitespace character")
        return value


# /ner and /annotate enpoint serializer request. 
class NERRequestList(BaseModel):
    data: List[TextItem] = Field(
        ..., 
        min_length=1,
        max_length=MAX_DATA_BY_REQUEST,
        description=f"List of text items (text + id), max {MAX_DATA_BY_REQUEST}"
    )
    allowed_types: List[str] = Field(
        default_factory=list,
        alias="types",
        description="Allowed types (from spacy). Default = [] (all)"
    )
    lang: str = Field(
        ...,
        description="Language code of the texts, ISO-639-1",
        json_schema_extra={"enum": list(ALLOWED_LANG_MODELS.keys())},
    )

    @field_validator("allowed_types", mode="before")
    @classmethod
    def check_empty_strings(cls, items):
        if any(not isinstance(item, str) or not item.strip() for item in items):
            raise ValueError("Empty strings are not allowed in 'allowed_types'")
        return items

    @field_validator("lang")
    @classmethod
    def check_supported_language(cls, value):
        if value not in ALLOWED_LANG_MODELS:
            raise ValueError(f"Unsupported language: {value}")
        return value


# ======================================================
# =====           RESPONSE SERIALIZER              =====
# ======================================================

# Entity structure
class Entity(BaseModel):
    name: str
    type: str
    start_offset: int
    end_offset: int

# /ner enpoint response serializer. 
class ResponseNER(BaseModel):
    id: Optional[str] = Field(None)
    entities: List[Entity]


# /annotate endpoint response serializer.
class ResponseAnnotate(BaseModel):
    id: Optional[str] = Field(None)
    annotated_text: str
    entities: List[Entity]
