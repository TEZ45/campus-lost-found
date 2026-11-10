import json
import math
import time

from django.core.management.base import BaseCommand, CommandError
from django.db import DatabaseError, close_old_connections

from apps.reports.notifications import deliver_pending


class Command(BaseCommand):
    help = "Deliver pending notices with durable retries. Run continuously or use --once."

    def add_arguments(self, parser):
        parser.add_argument("--once", action="store_true")
        parser.add_argument("--limit", type=int, default=100)
        parser.add_argument("--interval", type=float, default=1)

    def handle(self, *args, **options):
        if options["limit"] <= 0:
            raise CommandError("The batch limit must be positive.")
        if not math.isfinite(options["interval"]) or options["interval"] <= 0:
            raise CommandError("The polling interval must be positive and finite.")
        try:
            while True:
                try:
                    result = deliver_pending(options["limit"])
                except DatabaseError as exc:
                    if options["once"]:
                        raise CommandError("The notification database is unavailable.") from exc
                    result = {"error": type(exc).__name__}
                self.stdout.write(json.dumps(result))
                if options["once"]:
                    break
                close_old_connections()
                time.sleep(options["interval"])
        except KeyboardInterrupt:
            return
