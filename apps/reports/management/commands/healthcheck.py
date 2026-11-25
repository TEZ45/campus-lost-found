import json

from django.core.management.base import BaseCommand, CommandError

from apps.reports.operations import health_status


class Command(BaseCommand):
    help = "Read service health and task heartbeats without changing business data."

    def handle(self, *args, **options):
        result = health_status()
        self.stdout.write(json.dumps(result, sort_keys=True))
        if not result["ok"]:
            raise CommandError("Service health requires attention.")
