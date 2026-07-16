"""Dependency-free local server for the Playersim statistics workbench.

Run this file directly from any working directory.  The viewer deliberately
uses only Python's standard library so a fresh Playersim environment can open
the dashboard without a separate Dash, Flask, Node, or React installation.
"""

from __future__ import annotations

import argparse
import json
import mimetypes
import threading
import webbrowser
from http import HTTPStatus
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from pathlib import Path
from typing import Any, Callable
from urllib.parse import parse_qs, urlsplit

try:  # Package import used by tests and ``python -m``.
    from .viewer_data import ViewerRepository
except ImportError:  # Direct script execution.
    from viewer_data import ViewerRepository


STATIC_FILES = {
    "/": ("index.html", "text/html; charset=utf-8"),
    "/index.html": ("index.html", "text/html; charset=utf-8"),
    "/viewer.css": ("viewer.css", "text/css; charset=utf-8"),
    "/viewer.js": ("viewer.js", "text/javascript; charset=utf-8"),
}


def _one(query: dict[str, list[str]], key: str, default: str = "") -> str:
    values = query.get(key)
    return values[0] if values else default


def _bounded_int(value: str, default: int, minimum: int, maximum: int) -> int:
    try:
        parsed = int(value)
    except (TypeError, ValueError):
        return default
    return max(minimum, min(maximum, parsed))


def make_handler(repository: ViewerRepository, static_dir: Path):
    """Create an isolated request handler bound to one repository instance."""

    class ViewerRequestHandler(BaseHTTPRequestHandler):
        server_version = "PlayersimStatsViewer/2"

        def log_message(self, format_string: str, *args: Any) -> None:
            # Keep the terminal useful: only errors are emitted by default.
            if args and str(args[1]).startswith(("4", "5")):
                super().log_message(format_string, *args)

        def _json(self, payload: Any, status: int = HTTPStatus.OK) -> None:
            body = json.dumps(
                payload, ensure_ascii=False, allow_nan=False,
                separators=(",", ":"), default=str,
            ).encode("utf-8")
            self.send_response(status)
            self.send_header("Content-Type", "application/json; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.end_headers()
            self.wfile.write(body)

        def _error(self, status: int, message: str) -> None:
            self._json({"error": message, "status": int(status)}, status)

        def _static(self, request_path: str) -> bool:
            item = STATIC_FILES.get(request_path)
            if item is None:
                return False
            filename, content_type = item
            path = (static_dir / filename).resolve()
            if path.parent != static_dir.resolve() or not path.is_file():
                self._error(HTTPStatus.NOT_FOUND, "Static asset not found")
                return True
            body = path.read_bytes()
            self.send_response(HTTPStatus.OK)
            self.send_header(
                "Content-Type",
                content_type or mimetypes.guess_type(path.name)[0]
                or "application/octet-stream",
            )
            self.send_header("Content-Length", str(len(body)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self'; script-src 'self'; style-src 'self'; "
                "connect-src 'self'; img-src 'self' data:; object-src 'none'; "
                "base-uri 'none'; frame-ancestors 'none'",
            )
            self.send_header("Referrer-Policy", "no-referrer")
            self.end_headers()
            self.wfile.write(body)
            return True

        def do_GET(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlsplit(self.path)
            if self._static(parsed.path):
                return
            query = parse_qs(parsed.query, keep_blank_values=True)
            routes: dict[str, Callable[[], Any]] = {
                "/api/health": lambda: {
                    "status": "ok", "viewer": "playersim-stats-v2"
                },
                "/api/overview": repository.overview,
                "/api/runs": lambda: {"items": repository.runs()},
                "/api/stats-sources": lambda: {
                    "items": repository.stats_sources()
                },
                "/api/harvests": lambda: {"items": repository.harvests()},
                "/api/run": lambda: repository.run_detail(
                    _one(query, "run_id")
                ),
                "/api/evaluation-games": lambda: {
                    "items": repository.evaluation_games(
                        _one(query, "run_id"), include_debug=False
                    )
                },
                "/api/evaluation-game-debug": lambda: (
                    repository.evaluation_game_debug(
                        _one(query, "run_id"),
                        _bounded_int(
                            _one(query, "timestep"), -1, -1, 2_147_483_647),
                        _bounded_int(
                            _one(query, "case_index"), -1, -1, 10_000_000),
                        checkpoint_sha256=_one(
                            query, "checkpoint_sha256") or None,
                        record_id=_one(query, "record_id") or None,
                    )
                ),
                "/api/stats": lambda: repository.stats_bundle(
                    _one(query, "source_id")
                ),
                "/api/stats-games": lambda: repository.stats_games(
                    _one(query, "source_id"),
                    offset=_bounded_int(
                        _one(query, "offset"), 0, 0, 10_000_000
                    ),
                    limit=_bounded_int(
                        _one(query, "limit"), 200, 1, 500
                    ),
                ),
            }
            route = routes.get(parsed.path)
            if route is None:
                self._error(HTTPStatus.NOT_FOUND, "Unknown endpoint")
                return
            try:
                self._json(route())
            except KeyError as error:
                self._error(HTTPStatus.NOT_FOUND, str(error))
            except ValueError as error:
                self._error(HTTPStatus.BAD_REQUEST, str(error))
            except Exception as error:  # Local diagnostic tool: expose cause.
                self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"{type(error).__name__}: {error}",
                )

        def do_POST(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlsplit(self.path)
            if parsed.path != "/api/refresh":
                self._error(HTTPStatus.NOT_FOUND, "Unknown endpoint")
                return
            try:
                repository.refresh()
                self._json(repository.overview())
            except Exception as error:
                self._error(
                    HTTPStatus.INTERNAL_SERVER_ERROR,
                    f"{type(error).__name__}: {error}",
                )

        def do_HEAD(self) -> None:  # noqa: N802 - BaseHTTPRequestHandler API
            parsed = urlsplit(self.path)
            item = STATIC_FILES.get(parsed.path)
            if item is None:
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            filename, content_type = item
            path = static_dir / filename
            if not path.is_file():
                self.send_error(HTTPStatus.NOT_FOUND)
                return
            self.send_response(HTTPStatus.OK)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(path.stat().st_size))
            self.end_headers()

    return ViewerRequestHandler


def create_server(
    project_root: str | Path,
    host: str = "127.0.0.1",
    port: int = 8050,
) -> tuple[ThreadingHTTPServer, ViewerRepository]:
    """Build a server without starting it, allowing headless HTTP tests."""
    root = Path(project_root).expanduser().resolve()
    repository = ViewerRepository(root)
    static_dir = Path(__file__).resolve().parent / "static"
    server = ThreadingHTTPServer(
        (host, int(port)), make_handler(repository, static_dir)
    )
    server.daemon_threads = True
    return server, repository


def build_parser() -> argparse.ArgumentParser:
    parser = argparse.ArgumentParser(
        description="Open the unified Playersim run and DeckStats workbench."
    )
    parser.add_argument(
        "--root",
        default=str(Path(__file__).resolve().parents[1]),
        help="Playersim project root (auto-detected by default)",
    )
    parser.add_argument("--host", default="127.0.0.1")
    parser.add_argument("--port", type=int, default=8050)
    parser.add_argument(
        "--no-browser", action="store_true",
        help="Serve without opening the default browser",
    )
    parser.add_argument(
        "--check", action="store_true",
        help="Print discovered artifact counts and exit",
    )
    return parser


def main(argv: list[str] | None = None) -> int:
    args = build_parser().parse_args(argv)
    server, repository = create_server(args.root, args.host, args.port)
    if args.check:
        print(json.dumps(repository.overview(), indent=2, default=str))
        server.server_close()
        return 0

    address, port = server.server_address[:2]
    display_host = "127.0.0.1" if address in {"0.0.0.0", "::"} else address
    url = f"http://{display_host}:{port}/"
    print(f"Playersim Stats Workbench: {url}")
    print(f"Project root: {Path(args.root).expanduser().resolve()}")
    print("Press Ctrl+C to stop.")
    if not args.no_browser:
        threading.Timer(0.35, lambda: webbrowser.open(url)).start()
    try:
        server.serve_forever(poll_interval=0.25)
    except KeyboardInterrupt:
        print("\nStopping viewer.")
    finally:
        server.server_close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
