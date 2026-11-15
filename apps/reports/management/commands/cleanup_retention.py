import json

from django.core.management.base import BaseCommand, CommandError

from apps.reports.lifecycle import cleanup_retention


class Command(BaseCommand):
    help = "Preview retention cleanup; use --apply to erase eligible records and files."

    def add_arguments(self, parser):
        parser.add_argument("--apply", action="store_true")
        parser.add_argument("--batch-size", type=int, default=100)

    def handle(self, *args, **options):
        try:
            result = cleanup_retention(apply=options["apply"], batch_size=options["batch_size"])
        except ValueError as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
