"""Run the bounded WSGI server behind a local trusted ingress."""

import argparse
import os
import sys
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parents[1]))
os.environ.setdefault("DJANGO_SETTINGS_MODULE", "config.settings")

from django.conf import settings
from django.core.wsgi import get_wsgi_application
from waitress import serve


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    parser.add_argument("--port", type=int, default=8000)
    parser.add_argument("--threads", type=int, default=16)
    args = parser.parse_args()
    if not 1 <= args.port <= 65535 or not 1 <= args.threads <= 64:
        parser.error("Use port 1..65535 and threads 1..64.")
    app = get_wsgi_application()
    trust = bool(settings.SECURE_PROXY_SSL_HEADER)
    serve(
        app, host="127.0.0.1", port=args.port, threads=args.threads,
        connection_limit=250, backlog=128, channel_timeout=30, cleanup_interval=5,
        inbuf_overflow=256 * 1024, outbuf_overflow=256 * 1024,
        max_request_body_size=settings.REQUEST_MAX_BODY_BYTES,
        max_request_header_size=32768, ident="campus",
        trusted_proxy="127.0.0.1" if trust else None,
        trusted_proxy_headers={"x-forwarded-for", "x-forwarded-proto"} if trust else set(),
        **({"trusted_proxy_count": 1} if trust else {}),
    )


if __name__ == "__main__":
    main()
