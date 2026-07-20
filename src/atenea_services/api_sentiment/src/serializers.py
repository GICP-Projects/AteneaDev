from settings import MAX_DATA_BY_REQUEST
from pydantic import BaseModel, Field, field_validator
from typing import Dict, List


# ======================================================
# =====            REQUEST SERIALIZER              =====
# ======================================================

class TextItem(BaseModel):
    text: str = Field(..., min_length=1)

    @field_validator("text")
    @classmethod
    def check_text_has_content(cls, value):
        if not value.strip():
            raise ValueError("Text must contain at least one non-whitespace character")
        return value

class RequestSentiments(BaseModel):
    data: List[TextItem] = Field(
        ..., 
        min_length=1,
        max_length=MAX_DATA_BY_REQUEST,
        description=f"List of data items (text), max {MAX_DATA_BY_REQUEST}"
    )

# ======================================================
# =====           RESPONSE SERIALIZER              =====
# ======================================================

# /sentiment enpoint response serializer. 
class ResponseSentiments(BaseModel):
    """
    model: str
        The model used to predict the sentiment.
    version: str
        The version of the previous model.
    labels: dict
        The dictionary with the corresponding label to the value of each sentiment 
        prediction.
    sentiments:
        Prediction results (each result type is equivalent to a specific sentiment 
        given by the previous field `labels`).
    
    """
    model: str
    version: str
    labels: Dict[int, str]
    sentiments: List[int]
