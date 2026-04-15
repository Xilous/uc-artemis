"""UC Artemis entry point.

Launches a local Flask server and opens the user's default browser. Single
user, single Flask process — no auth, no multi-tenant concerns.
"""

from __future__ import annotations

import argparse
import sys
import threading
import webbrowser

from web.server import create_app


def parse_args() -> argparse.Namespace:
    p = argparse.ArgumentParser(description="UC Artemis — Bluebeam markup batch tool")
    p.add_argument("--port", type=int, default=5000, help="Local port (default: 5000)")
    p.add_argument("--no-browser", action="store_true", help="Do not auto-open the browser")
    return p.parse_args()


def main() -> int:
    args = parse_args()
    app = create_app()

    url = f"http://127.0.0.1:{args.port}/"
    if not args.no_browser:
        # Open the browser shortly after the server starts. Threading here is
        # only to avoid a potential race where the browser opens before Flask
        # is bound to the port; a 0.5 s delay is enough on every system tested.
        threading.Timer(0.5, lambda: webbrowser.open(url)).start()

    print(f"UC Artemis running at {url}")
    print("Press Ctrl+C to stop.")
    try:
        app.run(host="127.0.0.1", port=args.port, threaded=True, use_reloader=False)
    except KeyboardInterrupt:
        print("\nShutting down.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
