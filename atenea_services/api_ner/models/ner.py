"""This module includes NER functionalities."""
import spacy
import urllib.parse
from src.serializers import TextItem

class NerAPI():
    """Ner API which uses flair models."""

    def __init__(self, spacy_model_name, disable_pipes=[]):
        self.nlp = spacy.load(spacy_model_name, disable=disable_pipes)

    def ner(
        self, 
        text_items: TextItem, 
        allowed_types = [],
        annotate_text = False
    ):
        """Run NER to a text and return relevant entities.
        
        Parameters
        ----------
        self: self
            Reference of the instance

        text_items: models.serializers.TextItem
            Text item to analyze an extract its entities. Structure:
            {
                "text": ...
                "id": [... | None] (It's optional, but must contain null in that case)
            }

        allowed_types: list, default=[] (All)
            List/tuples of entities to be taken into consideration in the result.

        annotate_text: bool, default=False
            Flag to request for text annotation.

        Returns
        ----------
        ret: List[dict]
            List of text data with the following information:
            {   
                "id": str - given id
                "annotated_text": str - (Optional) Only if annotate_text is set to True
                "entities": [
                    {
                        "name": str - Entity Name
                        "type": str - Entity type
                        "start_offset": int - The first index where the entity starts in the text
                        "end_offset": int - The last index where the entity starts in the text
                    }
                    ...
                ]
            }

        """

        data = [(item.text, item.id) for item in text_items]
        entities = [
            {"id": id, **self._prepare_data(doc, allowed_types, annotate_text)}
            for doc, id in self.nlp.pipe(data, as_tuples=True)
        ]


        return entities

    def _prepare_data(self, doc, allowed_types = [], annotate_text = False):
        """
        Prepare the result data, if annotate_text is set to true it annotates the 
        provided text (inside the doc, doc.text) using the given entities (doc.ents).
        
        In that case it scans through the text and for each entity, replaces its 
        occurrence in the text with a formatted string using its name and type.
        If there's no entity in the text or if entities list is empty, the 
        original text is returned.
        
        Parameters
        ----------
        self : reference
            Reference to the current instance of the class.
            
        doc : spacy.tokens.doc.Doc
            The Spacy doc with the text info and its entities.
        
        allowed_types: list, default=[] (All)
            List/tuples of entities to be taken into consideration in the result.

        annotate_text: bool, default=False
            Flag to request for text annotation.

        Returns
        -------
        ret: dict
            Dict with the following structure depending if annotate_text flag was True/False
            annotate_text == True:
            {
                "entities": [
                    {
                        "name": str - Entity Name
                        "type": str - Entity type
                        "start_offset": int - The first index where the entity starts in the text
                        "end_offset": int - The last index where the entity starts in the text
                    },
                    ...
                ]
            }
            annotate_text == False
            {
                "entities": ...
                "annotated_text": "..." If no annotations are made, the original text is returned.
            }
        
        Examples
        --------
        >>> doc = nlp("Hello from New York.")
        >>> _prepare_data(doc, annotate_text=False)
        {
            "entities": [{"name": "New York", "type": "GPE", "start_offset": 11, "end_offset": 19}],
            "annotated_text": "Hello from [New York](New%20York&GPE)."
        }
        """
        annotated_text = ""
        entities = []
        last_offset = 0
        for ent in doc.ents:
            if not allowed_types or ent.label_ in allowed_types:
                ent_name = ent.text.strip()
                entities.append(
                    {
                        "name": ent_name,
                        "type": ent.label_,
                        "start_offset": ent.start_char,
                        "end_offset": ent.end_char,
                    } 
                )

                if annotate_text:
                    start, end = ent.start_char, ent.end_char
                    encoded_string = f"[{ent_name}]({urllib.parse.quote(ent_name)}&{urllib.parse.quote(ent.label_)})"
                    annotated_text += doc.text[last_offset:start] + encoded_string 
                    last_offset = end # Store the last position after the annotated entity (to avoid losing text)

        # Finally, copy the remaining text after the last entity.
        if annotate_text:
            if last_offset < len(doc.text):
                # If there are no entities this code will copy all the text into the result
                annotated_text += doc.text[last_offset:]

            return {"entities": entities, "annotated_text": annotated_text}
        
        return {"entities": entities}



## PRUEBAS ##
#from pydantic import BaseModel, Field
#from typing import Optional

#class TextItem(BaseModel):
#    id: Optional[str] = Field(None) 
#    text: str

#texto = "La empresa Apple ha facturado 50M de dólares más que Microsoft en el tercer trimestre de 2020."
#texto = """🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥🔥\n\n¿Crees que las administraciones publicas pueden exigir "la lealtad" a los médicos, censurar, amenazar con despidos y con sanciones por ser fieles al código deontológico? Por preocuparse por
# sus pacientes y darles la información sobre los riesgos de la "vacunación" o simplemente por opinar desde su conocimiento y experiencia como profesionales de salud?\nPor tener otra visión distinta de la emitida por canales de "inf
#ormación"? Esto está sucediendo aquí y ahora, Islas Baleares.\n\nEl ministerio de Sanidad está llevando a cabo una campaña de "vacunacion" ocultando lo más importante :\n-Que no es una vacuna tradicional. \n-Que estos fármacos géni
#cos están en el proceso de experimentacion y tienen muchísimos riesgos muy graves en comparación con el hipotético beneficio que pretenden conseguir. \n-que la situación "epidemiologica" se mide con herramientas dudosas con los tes
#t PCR que no pueden diagnosticar el contagio y se están realizando las denuncias de fraude a nivel internacional."""
#api = NerAPI('es_core_news_lg')
#res = api.ner([TextItem(id="12345", text=texto)], annotate_text=True)
#print(res)