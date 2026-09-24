"""Offline end-to-end test for the vision-only importer.

Stands up a tiny fake "游戏助手服务端" vision service on a random port and
runs sync_images_from_json against it, then checks the produced scene_map.json.

Run:  python tests/test_importer_smoke.py
"""

from __future__ import annotations

import json
import json as _jsonmod
import socket
import sys
import tempfile
import threading
import types
import urllib.error
import urllib.parse
import urllib.request
import uuid
from pathlib import Path
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

PROJECT_ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(PROJECT_ROOT))

# ---- requests-compatible stand-in (urllib backed) so the offline test can run
# ---- without a third-party install. Used only by this test process.


class _Response:
    def __init__(self, data: bytes, status: int, url: str = ""):
        self.content = data
        self.text = data.decode("utf-8", "replace")
        self.status_code = status
        self.url = url

    def raise_for_status(self):
        if self.status_code >= 400:
            raise RuntimeError(f"http {self.status_code}")

    def json(self):
        return _jsonmod.loads(self.text)

    def iter_lines(self, decode_unicode=False):
        for line in self.text.splitlines():
            yield line if decode_unicode else line.encode("utf-8")


def _build_multipart(data, files):
    boundary = "----gwa-test-" + uuid.uuid4().hex
    parts = []
    for key, value in data or []:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{key}"\r\n\r\n{value}\r\n'.encode("utf-8")
        )
    for field, (filename, content, ctype) in files or []:
        parts.append(
            f'--{boundary}\r\nContent-Disposition: form-data; name="{field}"; filename="{filename}"\r\n'
            f"Content-Type: {ctype}\r\n\r\n".encode("utf-8")
        )
        parts.append(content)
        parts.append(b"\r\n")
    parts.append(f"--{boundary}--\r\n".encode("utf-8"))
    return b"".join(parts), f"multipart/form-data; boundary={boundary}"


def _encode_url(url: str) -> str:
    parts = urllib.parse.urlsplit(url)
    path = urllib.parse.quote(parts.path)
    query = urllib.parse.quote(parts.query, safe="=&?%")
    return urllib.parse.urlunsplit((parts.scheme, parts.netloc, path, query, parts.fragment))


class _Session:
    headers = {}

    def get(self, url, timeout=None, **kwargs):
        req = urllib.request.Request(_encode_url(url), method="GET")
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _Response(resp.read(), resp.status, url)

    def post(self, url, data=None, json=None, files=None, headers=None, stream=False, timeout=None):
        body = None
        ctype = None
        if files is not None:
            body, ctype = _build_multipart(data, files)
        elif json is not None:
            body = _jsonmod.dumps(json, ensure_ascii=False).encode("utf-8")
            ctype = "application/json"
        elif isinstance(data, list):
            body = urllib.parse.urlencode(data).encode("utf-8")
            ctype = "application/x-www-form-urlencoded"
        elif data is not None:
            body = data
        req = urllib.request.Request(_encode_url(url), data=body, method="POST")
        if ctype:
            req.add_header("Content-Type", ctype)
        with urllib.request.urlopen(req, timeout=timeout) as resp:
            return _Response(resp.read(), resp.status, url)

    def close(self):
        pass


_requests = types.ModuleType("requests")
_requests.Session = _Session
sys.modules["requests"] = _requests


class _FakeVisionService:
    """Emulates the vision endpoints used by the importer."""

    def __init__(self) -> None:
        self.instances: set[str] = set()
        self.scenes: dict[str, list[str]] = {}
        self.inserted: list[tuple[str, str, list[str]]] = []
        self.build_calls = 0

    def handle(self, handler: BaseHTTPRequestHandler) -> None:
        path = urllib.parse.unquote(handler.path)
        method = handler.command
        length = int(handler.headers.get("Content-Length") or 0)
        if length:
            handler.rfile.read(length)

        if method == "GET" and path == "/vision/service/list":
            self._json(handler, {"code": "ok", "data": {"instances_id": list(self.instances)}})
        elif method == "POST" and path.startswith("/vision/service/init/"):
            instance = path.rsplit("/", 1)[-1]
            self.instances.add(instance)
            self.scenes.setdefault(instance, [])
            self._json(handler, {"code": "ok"})
        elif method == "GET" and path.startswith("/vision/scene/list/"):
            instance = path.rsplit("/", 1)[-1]
            self._json(handler, {"code": "ok", "data": {"scenes_id": list(self.scenes.get(instance, []))}})
        elif method == "POST" and "/vision/scene/insert/" in path:
            rest = path[len("/vision/scene/insert/"):]
            instance, scene_id = rest.split("/", 1)
            self.scenes.setdefault(instance, []).append(scene_id)
            self.inserted.append((instance, scene_id, []))
            self._json(handler, {"code": "ok", "data": {"invalid_pictures": []}})
        elif method == "POST" and path.startswith("/vision/service/build/"):
            self.build_calls += 1
            self._sse(
                handler,
                [
                    {"code": "ok", "data": {"event_name": "build_progress", "progress": 1, "total": 1}},
                    {"code": "ok", "data": {"event_name": "build_done"}},
                ],
            )
        else:
            handler.send_response(404)
            handler.end_headers()
            handler.wfile.write(b"{}")

    @staticmethod
    def _json(handler: BaseHTTPRequestHandler, payload: dict) -> None:
        body = json.dumps(payload, ensure_ascii=False).encode("utf-8")
        handler.send_response(200)
        handler.send_header("Content-Type", "application/json")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)

    @staticmethod
    def _sse(handler: BaseHTTPRequestHandler, events: list[dict]) -> None:
        body = b"".join(
            f"data: {json.dumps(event, ensure_ascii=False)}\n\n".encode("utf-8")
            for event in events
        )
        handler.send_response(200)
        handler.send_header("Content-Type", "text/event-stream")
        handler.send_header("Content-Length", str(len(body)))
        handler.end_headers()
        handler.wfile.write(body)
        handler.wfile.flush()


class _Handler(BaseHTTPRequestHandler):
    def log_message(self, fmt, *args):
        pass

    def do_GET(self):
        self.server.service.handle(self)

    def do_POST(self):
        self.server.service.handle(self)


def free_port() -> int:
    with socket.socket() as s:
        s.bind(("127.0.0.1", 0))
        return s.getsockname()[1]


def test_import() -> None:
    service = _FakeVisionService()
    httpd = ThreadingHTTPServer(("127.0.0.1", 0), _Handler)
    httpd.service = service  # type: ignore[attr-defined]
    port = httpd.server_address[1]
    threading.Thread(target=httpd.serve_forever, daemon=True).start()

    tmp = Path(tempfile.mkdtemp(prefix="gwa-import-"))
    game_dir = tmp / "测试游戏"
    (game_dir / "pages" / "page_1" / "images").mkdir(parents=True)
    (game_dir / "pages" / "page_2" / "images").mkdir(parents=True)
    img1 = game_dir / "pages" / "page_1" / "images" / "image_0001.jpg"
    img2 = game_dir / "pages" / "page_1" / "images" / "image_0002.jpg"
    img1.write_bytes(b"\xff\xd8fake1")
    img2.write_bytes(b"\xff\xd8fake2")
    img3 = game_dir / "pages" / "page_2" / "images" / "image_0001.jpg"
    img3.write_bytes(b"\xff\xd8fake3")

    images_json = game_dir / "images.json"
    images_json.write_text(
        json.dumps(
            {
                "game": "测试游戏",
                "pages": [
                    {
                        "url": "http://example.com/page1.html",
                        "page_index": 1,
                        "images": [
                            {"src": "http://example.com/img1.jpg", "local": "测试游戏/pages/page_1/images/image_0001.jpg", "index": 0},
                            {"src": "http://example.com/img2.jpg", "local": "测试游戏/pages/page_1/images/image_0002.jpg", "index": 1},
                        ],
                    },
                    {
                        "url": "http://example.com/page2.html",
                        "page_index": 2,
                        "images": [
                            {"src": "http://example.com/img3.jpg", "local": "测试游戏/pages/page_2/images/image_0001.jpg", "index": 0},
                        ],
                    },
                ],
            },
            ensure_ascii=False,
        ),
        encoding="utf-8",
    )

    from app.walkthrough_service_importer import WalkthroughServiceImporter

    importer = WalkthroughServiceImporter(host=f"127.0.0.1:{port}", timeout=10)
    progress_lines: list[str] = []
    try:
        result = importer.sync_images_from_json(
            instance_id="测试游戏",
            images_json_path=images_json,
            force_reimport=False,
            progress_callback=progress_lines.append,
        )
    finally:
        importer.close()

    assert result["scenes_total"] == 2
    assert result["scenes_inserted"] == 2
    assert service.build_calls == 1
    assert set(service.scenes["测试游戏"]) == set(result_scene_ids(result))
    # 进度回调：插入与构建阶段都有实时进度行（供客户端下载进度横幅解析）
    assert any(line.startswith("[vision] insert ") and "1/2" in line for line in progress_lines), progress_lines
    assert any(line.startswith("[vision] build ") and "1/1" in line for line in progress_lines), progress_lines
    assert progress_lines[0] == "[vision] inserting 2 scene(s)", progress_lines

    assert result["scenes_total"] == 2
    assert result["scenes_inserted"] == 2
    assert service.build_calls == 1
    assert set(service.scenes["测试游戏"]) == set(result_scene_ids(result))

    scene_map = json.loads((game_dir / "scene_map.json").read_text(encoding="utf-8"))
    assert scene_map["instance_id"] == "测试游戏"
    assert len(scene_map["scenes"]) == 2
    pt = scene_map["picture_to_scene"]
    assert "测试游戏/pages/page_1/images/image_0001.jpg" in pt
    assert pt["测试游戏/pages/page_1/images/image_0001.jpg"]["src"] == "http://example.com/img1.jpg"
    assert pt["测试游戏/pages/page_1/images/image_0001.jpg"]["index"] == 0
    # basename fallback key
    assert pt["image_0002.jpg"]["src"] == "http://example.com/img2.jpg"

    # re-import: no new scenes, build still runs
    importer2 = WalkthroughServiceImporter(host=f"127.0.0.1:{port}", timeout=10)
    try:
        result2 = importer2.sync_images_from_json(instance_id="测试游戏", images_json_path=images_json)
    finally:
        importer2.close()
    assert result2["scenes_inserted"] == 0
    assert result2["scenes_skipped"] == 2

    httpd.shutdown()
    print("IMPORTER E2E TEST PASSED")


def result_scene_ids(result: dict) -> list[str]:
    import hashlib

    return [
        hashlib.sha256(url.encode("utf-8")).hexdigest()
        for url in ("http://example.com/page1.html", "http://example.com/page2.html")
    ]


if __name__ == "__main__":
    test_import()
