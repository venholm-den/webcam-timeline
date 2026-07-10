from __future__ import annotations

import argparse
import socket
from functools import partial
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path


PROJECT_ROOT = Path(__file__).resolve().parents[1]


class TimelineHandler(SimpleHTTPRequestHandler):
    extensions_map = {
        **SimpleHTTPRequestHandler.extensions_map,
        ".csv": "text/csv; charset=utf-8",
        ".html": "text/html; charset=utf-8",
        ".js": "text/javascript; charset=utf-8",
    }

    def end_headers(self) -> None:
        path = self.path.split("?", 1)[0].lower()

        if path.endswith((".html", ".csv")):
            self.send_header("Cache-Control", "no-store, max-age=0")
        elif "/data/images/" in path:
            self.send_header("Cache-Control", "public, max-age=86400")

        self.send_header("X-Content-Type-Options", "nosniff")
        super().end_headers()


def local_ip_addresses() -> list[str]:
    addresses: set[str] = set()
    hostname = socket.gethostname()

    try:
        for item in socket.getaddrinfo(hostname, None, socket.AF_INET):
            address = item[4][0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass

    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect(("8.8.8.8", 80))
            address = sock.getsockname()[0]
            if not address.startswith("127."):
                addresses.add(address)
    except OSError:
        pass

    return sorted(addresses)


def main() -> int:
    parser = argparse.ArgumentParser(
        description="Serve the webcam timeline history from this machine."
    )
    parser.add_argument(
        "--host",
        default="0.0.0.0",
        help="Address to bind to. Use 0.0.0.0 for other devices on your network.",
    )
    parser.add_argument("--port", type=int, default=8000, help="Port to listen on.")
    args = parser.parse_args()

    handler = partial(TimelineHandler, directory=str(PROJECT_ROOT))
    server = ThreadingHTTPServer((args.host, args.port), handler)

    print("Serving webcam timeline history")
    print(f"Project: {PROJECT_ROOT}")
    print(f"Local:   http://127.0.0.1:{args.port}/data/timeline.html")

    for address in local_ip_addresses():
        print(f"Network: http://{address}:{args.port}/data/timeline.html")

    print()
    print("Leave this window open while people are viewing the history.")
    print("Press Ctrl+C to stop.")

    try:
        server.serve_forever()
    except KeyboardInterrupt:
        print("\nStopped.")
    finally:
        server.server_close()

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
