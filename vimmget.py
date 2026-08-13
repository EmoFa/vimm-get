#!/usr/bin/env python3
"""Start the VimmGet web app and open it in your browser.

    python vimmget.py            # default: http://127.0.0.1:8317
    python vimmget.py --port 0   # pick any free port
    python vimmget.py --no-open  # don't open the browser
"""

import argparse
import socket
import threading
import webbrowser

import uvicorn

from vimm.server import create_app


def main() -> int:
    parser = argparse.ArgumentParser()
    parser.add_argument("--port", type=int, default=8317)
    parser.add_argument("--no-open", action="store_true")
    args = parser.parse_args()

    port = args.port
    if port == 0:
        with socket.socket() as probe:
            probe.bind(("127.0.0.1", 0))
            port = probe.getsockname()[1]

    url = f"http://127.0.0.1:{port}"
    print(f"  VimmGet:  {url}   (Ctrl+C to quit)")
    if not args.no_open:
        threading.Timer(0.8, webbrowser.open, args=(url,)).start()

    uvicorn.run(create_app(), host="127.0.0.1", port=port, log_level="warning")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
