from django.db import models
from django_elasticsearch_dsl.signals import BaseSignalProcessor


class OnlyDeleteSignalES(BaseSignalProcessor):
    """Real-time signal processor which only will observe deletes actions for
    Elasticsearch index syncronization.

    Allows for observing when deletes fire and automatically updates the
    search engine index appropriately.
    """

    def setup(self):
        # Listen to all model deletes.
        models.signals.post_delete.connect(self.handle_delete)

        # Use to manage related objects update
        models.signals.pre_delete.connect(self.handle_pre_delete)

    def teardown(self):
        # Teardown all signals.
        models.signals.post_delete.disconnect(self.handle_delete)
        models.signals.pre_delete.disconnect(self.handle_pre_delete)