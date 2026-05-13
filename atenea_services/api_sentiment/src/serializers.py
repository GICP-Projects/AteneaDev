from settings import MAX_DATA_BY_REQUEST
from pydantic import BaseModel, Field, validator
from typing import Dict, List


# ======================================================
# =====            REQUEST SERIALIZER              =====
# ======================================================

class TextItem(BaseModel):
    text: str = Field(None, )#max_length=4096)

class RequestSentiments(BaseModel):
    data: List[TextItem] = Field(
        ..., 
        max_items=MAX_DATA_BY_REQUEST, 
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
