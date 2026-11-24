from django.core.management.base import BaseCommand

from app_telegram.clickhouse import ensure_schema, projection_lock, table_name


class Command(BaseCommand):
    help = "Create or verify the ClickHouse schema."

    def handle(self, **options):
        with projection_lock():
            ensure_schema()
        self.stdout.write(f"ClickHouse ready: {table_name()}")
