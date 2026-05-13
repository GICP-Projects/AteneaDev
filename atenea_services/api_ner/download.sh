#!/bin/bash

# Flair downloads
python3 -c "import spacy" 
EXIT_STATUS=$?
if [ "$EXIT_STATUS" -eq "1" ]
then
    echo "Spacy is not installed."
else 
    echo "Downloading Spacy models..."
    python -m spacy download es_core_news_lg # best ner accuracy spanish 
    python -m spacy download en_core_web_lg # best ner accuracy english (without trf)
    # TRANFORMED MODEL: VERY SLOWER AND EXPENSIVE WITH CPU
    #python -m spacy download en_core_web_trf # best ner accuracy english 
fi