import json

from django.core.exceptions import ValidationError
from django.core.management.base import BaseCommand, CommandError

from apps.reports.storage_quota import reconcile_storage


class Command(BaseCommand):
    help = "Preview media accounting; --apply recounts usage and queues bounded orphan cleanup."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--batch-size", type=int, default=100)
        parser.add_argument("--max-files", type=int, default=10000)

    def handle(self, *args, **options):
        if not 1 <= options["batch_size"] <= 1000 or options["max_files"] < 1:
            raise CommandError("Use batch size 1..1000 and a positive --max-files.")
        try:
            result = reconcile_storage(
                apply=options["apply"], max_files=options["max_files"],
                batch_size=options["batch_size"],
            )
        except (ValidationError, OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
