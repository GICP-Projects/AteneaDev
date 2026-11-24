"""Stable processor and stage identifiers owned by the entity app.

Keep this module free of Django/model imports: settings imports these keys
while the application registry is still being initialized.
"""

NER_PROCESSOR = "entity.ner"
NER_EXTRACT_STAGE = "entity.ner.extract"
NER_STORE_STAGE = "entity.ner.store"
NER_WORKFLOW = [NER_EXTRACT_STAGE, NER_STORE_STAGE]
