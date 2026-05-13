
import torch
from transformers import AutoTokenizer, AutoModelForSequenceClassification
from settings import DEVICE, TRUST_REMOTE_CODE


#DEVICE = "cuda" if torch.cuda.is_available() else "cpu"

class SentimentAPI():
    """ Base class 
    """

    def __init__(self, model_name, *args, **kwargs):
        pass

    def get_name(self):
        """ Get model name."""
        raise NotImplementedError('`get_name()` must be implemented.')

    def get_version(self):
        """ Get model version."""
        raise NotImplementedError('`get_version()` must be implemented.')

    def predict(self, input_texts):
        """ Get embeddings from a text. Must return a list of values and a dict with the labels."""
        raise NotImplementedError('`get_embeddings()` must be implemented.')
       
    @classmethod
    def clean_prediction(cls):
        torch.cuda.empty_cache()



class SentimentPipelineAPI(SentimentAPI):
    def __init__(self, model_name, max_length, *args, **kwargs):
        self.tokenizer = AutoTokenizer.from_pretrained(
            model_name, 
            trust_remote_code = TRUST_REMOTE_CODE
        )
        self.model = AutoModelForSequenceClassification.from_pretrained(
            model_name,
            torch_dtype=torch.float16,
            trust_remote_code = TRUST_REMOTE_CODE
        ).to(DEVICE)

        # Ensure the model is in evaluation mode
        self.model.eval()

    def get_name(self):
        return self.model.name_or_path

    def get_version(self):
        return self.model._version

    def predict(self, input_texts):
        """
        Predicts sentiment for the given input texts.
        #TODO optimization by batching (batch proccessing)

        Parameters
        ----------
        input_texts: List[str]
            Input text(s) for sentiment analysis.

        Returns:
        total_predictions: List[dict] 
            A list with the sentiment prediction values for each input text.
            ```
            ```
        labels: dict
            A dictionary with the sentiment labels. Values as keys and labels as values.
            ```
            {0: 'Negative', 1: 'Neutral', 2: 'Positive'}
            ```
        """
        total_predictions = []
        for text in input_texts:
            #tokenize an move to device
            inputs = self.tokenizer(text, return_tensors="pt").to(DEVICE)
            # Perform inference in inference mode
            with torch.inference_mode():
                outputs = self.model(**inputs)
                predictions = outputs.logits.argmax(dim=-1)
                total_predictions.append(predictions.item())
        return total_predictions, self.model.config.id2label

 

def get_model_class() -> SentimentAPI:
    """ Returns a SentimentAPI subclass
    """
    return SentimentPipelineAPI #if True MODEL_LIBRARY == 'transformers' else ...