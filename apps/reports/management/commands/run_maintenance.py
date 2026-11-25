import json

from django.core.management.base import BaseCommand, CommandError

from apps.reports.lifecycle import cleanup_retention
from apps.reports.operations import run_tracked
from apps.reports.storage_quota import process_file_deletions, reconcile_storage


class Command(BaseCommand):
    help = "Run one bounded maintenance batch and record a durable heartbeat."

    def add_arguments(self, parser):
        parser.add_argument("--task", required=True, choices=["retention", "deletions", "storage"])
        parser.add_argument("--batch-size", type=int, default=100)
        parser.add_argument("--max-files", type=int, default=100000)

    def handle(self, *args, **options):
        if not 1 <= options["batch_size"] <= 1000 or not 1 <= options["max_files"] <= 1000000:
            raise CommandError("Use batch size 1..1000 and max files 1..1000000.")
        operations = {
            "retention": lambda: cleanup_retention(apply=True, batch_size=options["batch_size"]),
            "deletions": lambda: process_file_deletions(batch_size=options["batch_size"]),
            "storage": lambda: reconcile_storage(
                apply=True, batch_size=options["batch_size"], max_files=options["max_files"],
            ),
        }
        try:
            result = run_tracked(options["task"], operations[options["task"]])
        except Exception as exc:  # noqa: BLE001 - Report a safe failure without private file paths.
            self.stdout.write(json.dumps({"ok": False, "task": options["task"], "error": type(exc).__name__}))
            raise CommandError("Maintenance failed; inspect service health.") from None
        ok = not any(result.get(key, 0) for key in ("files_failed", "files_referenced"))
        self.stdout.write(json.dumps({"ok": ok, "task": options["task"], "result": result}, sort_keys=True))
        if not ok:
            raise CommandError("Maintenance has pending file failures.")
