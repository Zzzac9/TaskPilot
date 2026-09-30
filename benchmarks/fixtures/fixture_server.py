"""带独立 side-effect state endpoint 的 localhost benchmark site。"""

import argparse
import json
from http.server import SimpleHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from threading import Lock


STATE = {"submit_count": 0, "delete_count": 0, "vision_count": 0, "last_profile": None}
LOCK = Lock()


class Handler(SimpleHTTPRequestHandler):
    def do_GET(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/fixture-state":
            with LOCK:
                body = json.dumps(STATE, sort_keys=True).encode()
            self.send_response(200)
            self.send_header("Content-Type", "application/json")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        super().do_GET()

    def do_POST(self) -> None:  # noqa: N802 - stdlib handler API
        if self.path == "/reset":
            with LOCK:
                STATE.update(
                    submit_count=0, delete_count=0, vision_count=0, last_profile=None
                )
            self._json_response({"reset": True})
            return
        if self.path not in {"/events/submit", "/events/delete", "/events/vision"}:
            self.send_error(404)
            return
        length = int(self.headers.get("Content-Length", "0"))
        payload = json.loads(self.rfile.read(length) or b"{}")
        with LOCK:
            if self.path.endswith("submit"):
                STATE["submit_count"] += 1
                STATE["last_profile"] = payload
            elif self.path.endswith("delete"):
                STATE["delete_count"] += 1
            else:
                STATE["vision_count"] += 1
        self._json_response({"recorded": True})

    def _json_response(self, value: object) -> None:
        body = json.dumps(value).encode()
        self.send_response(200)
        self.send_header("Content-Type", "application/json")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def log_message(self, format: str, *args: object) -> None:
        return None


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, required=True)
    args = parser.parse_args()
    site = Path(__file__).with_name("site")

    def handler(*handler_args: object, **kwargs: object) -> Handler:
        return Handler(*handler_args, directory=str(site), **kwargs)

    ThreadingHTTPServer((args.host, args.port), handler).serve_forever()


if __name__ == "__main__":
    main()
