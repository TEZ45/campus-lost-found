import json

from django.core.management.base import BaseCommand, CommandError

from apps.reports.recovery import change_recovery_state, recovery_status


class Command(BaseCommand):
    help = "Control collection after recovery using external, operator-owned state."

    def add_arguments(self, parser):
        parser.add_argument("action", choices=("status", "pause", "resume"))
        parser.add_argument("--operator")
        parser.add_argument("--reference")
        parser.add_argument("--quiesced", action="store_true")
        parser.add_argument("--custody-reconciled", action="store_true")

    def handle(self, *args, **options):
        try:
            if options["action"] == "status":
                result = recovery_status()
            else:
                result = change_recovery_state(
                    paused=options["action"] == "pause", operator=options["operator"],
                    reference=options["reference"], quiesced=options["quiesced"],
                    custody_reconciled=options["custody_reconciled"],
                )
        except (OSError, ValueError) as exc:
            raise CommandError(str(exc)) from exc
        self.stdout.write(json.dumps(result, sort_keys=True))
