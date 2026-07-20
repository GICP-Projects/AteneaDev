from types import SimpleNamespace
from unittest.mock import Mock, patch

from models.ner import NerAPI, normalize_entity_type
from src.serializers import TextItem


def entity(text, label, start, end):
    return SimpleNamespace(
        text=text,
        label_=label,
        start_char=start,
        end_char=end,
    )


def document(text, entities=()):
    return SimpleNamespace(text=text, ents=list(entities))


def test_constructor_loads_configured_spacy_pipeline():
    nlp = Mock()
    with patch("models.ner.spacy.load", return_value=nlp) as load:
        model = NerAPI("en_core_web_lg", ["parser"])

    assert model.nlp is nlp
    load.assert_called_once_with("en_core_web_lg", disable=["parser"])


def test_prepare_data_extracts_offsets_and_filters_types():
    model = NerAPI.__new__(NerAPI)
    doc = document(
        "Alice visited New York",
        [
            entity("Alice", "PERSON", 0, 5),
            entity("New York", "GPE", 14, 22),
        ],
    )

    result = model._prepare_data(doc, allowed_types=["GPE"])

    assert result == {
        "entities": [
            {
                "name": "New York",
                "type": "GPE",
                "start_offset": 14,
                "end_offset": 22,
            }
        ]
    }


def test_entity_type_aliases_use_atenea_canonical_names():
    assert normalize_entity_type("PERSON") == "PER"
    assert normalize_entity_type("PER") == "PER"
    assert normalize_entity_type("LOC") == "LOCATION"
    assert normalize_entity_type("GPE") == "GPE"


def test_prepare_data_normalizes_model_labels_before_filtering():
    model = NerAPI.__new__(NerAPI)
    doc = document(
        "Alice visited Madrid",
        [
            entity("Alice", "PERSON", 0, 5),
            entity("Madrid", "LOC", 14, 20),
        ],
    )

    result = model._prepare_data(doc, allowed_types=["PER", "LOCATION"])

    assert result == {
        "entities": [
            {
                "name": "Alice",
                "type": "PER",
                "start_offset": 0,
                "end_offset": 5,
            },
            {
                "name": "Madrid",
                "type": "LOCATION",
                "start_offset": 14,
                "end_offset": 20,
            },
        ]
    }


def test_prepare_data_accepts_native_aliases_but_returns_canonical_names():
    model = NerAPI.__new__(NerAPI)
    doc = document("Alice", [entity("Alice", "PERSON", 0, 5)])

    result = model._prepare_data(
        doc, allowed_types=["PERSON"], annotate_text=True
    )

    assert result["entities"][0]["type"] == "PER"
    assert result["annotated_text"] == "[Alice](Alice&PER)"


def test_prepare_data_keeps_misc_when_requested():
    model = NerAPI.__new__(NerAPI)
    doc = document("Project Atenea", [entity("Atenea", "MISC", 8, 14)])

    result = model._prepare_data(doc, allowed_types=["MISC"])

    assert result["entities"] == [
        {
            "name": "Atenea",
            "type": "MISC",
            "start_offset": 8,
            "end_offset": 14,
        }
    ]


def test_prepare_data_annotates_entities_and_url_encodes_names():
    model = NerAPI.__new__(NerAPI)
    doc = document(
        "Meet New York today",
        [entity("New York", "GPE", 5, 13)],
    )

    result = model._prepare_data(doc, annotate_text=True)

    assert result["annotated_text"] == "Meet [New York](New%20York&GPE) today"
    assert result["entities"][0]["start_offset"] == 5


def test_prepare_data_keeps_original_text_without_entities():
    model = NerAPI.__new__(NerAPI)

    result = model._prepare_data(document("No entities"), annotate_text=True)

    assert result == {"entities": [], "annotated_text": "No entities"}


def test_ner_preserves_batch_order_and_identifiers():
    first = document("Alice", [entity("Alice", "PERSON", 0, 5)])
    second = document("Madrid", [entity("Madrid", "GPE", 0, 6)])
    nlp = Mock()
    nlp.pipe.return_value = [(first, "one"), (second, "two")]
    model = NerAPI.__new__(NerAPI)
    model.nlp = nlp

    result = model.ner(
        [TextItem(id="one", text="Alice"), TextItem(id="two", text="Madrid")]
    )

    assert [item["id"] for item in result] == ["one", "two"]
    assert [item["entities"][0]["type"] for item in result] == ["PER", "GPE"]
    nlp.pipe.assert_called_once()
