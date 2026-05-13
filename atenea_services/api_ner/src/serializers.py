from settings import ALLOWED_LANG_MODELS, MAX_DATA_BY_REQUEST
from pydantic import BaseModel, Field, validator
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
    text: str


# /ner and /annotate enpoint serializer request. 
class NERRequestList(BaseModel):
    data: List[TextItem] = Field(
        ..., 
        max_items=MAX_DATA_BY_REQUEST, 
        description=f"List of text items (text + id), max {MAX_DATA_BY_REQUEST}"
    )
    allowed_types: List[str] = Field(
        default=[],
        alias="types",
        description="Allowed types (from spacy). Default = [] (all)"
    )
    lang: str = Field(
        ...,
        description="Language code of the texts, ISO-639-1",
        enum=list(ALLOWED_LANG_MODELS.keys())
    )

    @validator("allowed_types", pre=True, each_item=True)
    def check_empty_strings(cls, item):
        if item == "":
            raise ValueError("Empty strings are not allowed in 'allowed_types'")
        return item


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