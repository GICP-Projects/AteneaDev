# Atenea microservices

- api_ner: to make name entity recognition using spaCy.

- api_sentiment: to classify sentiment labels from text.

## API NER 
### Adding a New Language to API-NER

To extend the capabilities of the API-NER to support additional languages, follow the steps outlined below:

#### Step 1: Update `settings.py`

First, you need to specify the new language and its corresponding spaCy model in the `settings.py` file. This involves editing the `ALLOWED_LANG_MODELS` dictionary to include the ISO-639-1 code of the new language, the spaCy model to use, and an optional list of pipeline components to disable for improved performance, if necessary.

Here is an example of how to add a new language to `settings.py`:

```Python
# settings.py

# All spaCy models available for each allowed language (lang code must use ISO-639-1). 
# NOTE: Each used model must be added to the downloader script (download.sh)
ALLOWED_LANG_MODELS = {
    # LANG: (SPACY MODEL, DISABLE PIPELINES)
    "es": ("es_core_news_lg", []), # Unnecesary, very fast model 2secs for 480 items
    "en": ("en_core_web_lg", []),
    # Add your new language here (Example: "fr": ("fr_core_news_md", ["parser", "tagger"]))
    "NEW_LANG_CODE": ("YOUR_SPACY_MODEL", ["PIPELINE_TO_DISABLE"])
}
```

#### **Note** on Transformer Models:
While transformer models (trf) such as those included in the en_core_web_trf package offer excellent accuracy, they are not recommended for CPU-only environments due to their slower processing speed and higher computational costs for a relatively small performance improvement (around +2-3%).

#### Performance in a CPU: i7-1360P
| Model                | Examples                     | Time    |
|----------------------|------------------------------|---------|
| `en_core_web_sm`     | 500 texts (same texts)       | 28.7s   |
| `en_core_web_md`     | 5.000 texts (same texts)     | 22.92s  |


#### Step 2: Update the download.sh Script

After updating `settings.py`, you must ensure that the specified spaCy model is downloaded and available for use. This is done by adding the model to the `download.sh` script, which is responsible for downloading the necessary spaCy models.

Here's how to add the command to download the new language model in `download.sh`:

```bash
# download.sh
...
else 
echo "Downloading spaCy models..."
python -m spacy download es_core_news_lg # Best NER accuracy for Spanish 
python -m spacy download en_core_web_lg  # Best NER accuracy for English (without transformer models)
# Add the new language model here. Example:
python -m spacy download YOUR_SPACY_MODEL # Description of your model
...
```

## API EMBEDDINGS (moved out of `atenea_services`)
Embeddings are no longer served by an internal microservice in this repository.
Atenea now consumes embeddings through an OpenAI-compatible endpoint configured
from `atenea_api` (`OPENAI_EMBEDDINGS_*` and `QDRANT_*` variables in `.env.*`).
This means `api_embed` is not required in `COMPOSE_FILE`.

## API SENTIMENT 
Set in the `SENTIMENT_SERVICE_MODEL_NAME` variable the model name (from [Hugging Face sentiment analysis models](https://huggingface.co/models?pipeline_tag=text-classification)). **Advice** not all the models will work properly. Then, add in the `SENTIMENT_SERVICE_MODEL_MAX_TOKENS` variable the maximum number of tokens that the chosen model can handle.

With the `SENTIMENT_ROOT_PATH` variable, you can choose the root path of the endpoint that is going to be used for this microservice.

The variable `SENTIMENT_MODEL_PATH` points to the folder where the sentiment analysis model will be stored. Thus, the image and the container are lighter and it is not necessary to re-download the model at each image creation.

# Development deploy
Create a `.env` file copying the `.env.example` file. 

Set in the `COMPOSE_FILE` variable all the microservices you want to deploy by adding the `docker-compose.yml` file path of each one. It will contain all of them by default.

```.env
COMPOSE_PATH_SEPARATOR=:
COMPOSE_FILE=docker-compose.yml:api_ner/docker-compose.yml:api_sentiment/docker-compose.yml
```

Available microservices:
- NER: `api_ner/docker-compose.yml`
- SENTIMENT: `api_sentiment/docker-compose.yml`

Fill in all the enviroment variables of the microservices to be deployed.
```.env
NER_SERVICE_API_KEY="..."
...
## 
SENTIMENT_SERVICE_API_KEY="..."
...
```

Generate an API_KEY, for example, by using the UUID Python library:
```Python
import uuid
uuid.uuid4()
```

Copy the UUID into the `XXXX_SERVICE_API_KEY` variable. This will be the required API_KEY header that has to be sent with each request.
```Python
headers = {
    'ApiKey': XXXX_SERVICE_API_KEY
}
```

### Deployment command.

```bash
docker-compose -p atenea-dev up -d
```

Custom scale for load balancing using `--scale <service-name>` parameter
```bash
docker-compose -p atenea-dev up -d --scale ner-api=2 --scale sentiment-api-gpu1=3 --scale sentiment-api-gpu2=3
```
