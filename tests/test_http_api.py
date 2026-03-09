import json
import threading
import time
import unittest
import urllib.error
import urllib.request
import importlib.util
from pathlib import Path


def _load_bridge_module():
    p = Path(__file__).resolve().parents[1] / "scripts" / "bridge.py"
    spec = importlib.util.spec_from_file_location("ag_bridge", p)
    mod = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(mod)
    return mod


class _FakeBridge:
    def __init__(self):
        self.cdp = 9229
        self.model = "Claude Opus 4.6 (Thinking)"
        self.mc = 0

    def chat(self, p, to=300, m=None):
        return {"status": "ok", "response": f"echo:{p}", "elapsed": 0.0, "model": m or self.model}

    def switch(self, m):
        self.model = m
        return {"status": "ok", "model": m}

    def new_chat(self):
        self.mc = 0
        return {"status": "ok", "method": "reload", "ready": True}


class HttpApiTests(unittest.TestCase):
    @classmethod
    def setUpClass(cls):
        cls.mod = _load_bridge_module()
        cls.mod.b = _FakeBridge()
        cls.httpd = cls.mod.ThreadedHTTPServer(("127.0.0.1", 0), cls.mod.H)
        cls.port = cls.httpd.server_address[1]
        cls.t = threading.Thread(target=cls.httpd.serve_forever, daemon=True)
        cls.t.start()

    @classmethod
    def tearDownClass(cls):
        cls.httpd.shutdown()
        cls.httpd.server_close()

    def _url(self, path):
        return f"http://127.0.0.1:{self.port}{path}"

    def _post_raw(self, path, body_bytes):
        req = urllib.request.Request(self._url(path), method="POST", data=body_bytes)
        req.add_header("Content-Type", "application/json")
        return urllib.request.urlopen(req, timeout=3)

    def _post_json(self, path, payload):
        return self._post_raw(path, json.dumps(payload).encode("utf-8"))

    def test_chat_invalid_json_returns_400(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_raw("/chat", b"{")
        self.assertEqual(ctx.exception.code, 400)
        data = json.loads(ctx.exception.read().decode("utf-8"))
        self.assertEqual(data.get("status"), "error")

    def test_chat_bad_timeout_returns_400(self):
        with self.assertRaises(urllib.error.HTTPError) as ctx:
            self._post_json("/chat", {"prompt": "x", "timeout": "nope"})
        self.assertEqual(ctx.exception.code, 400)

    def test_chat_ok(self):
        resp = self._post_json("/chat", {"prompt": "hello", "timeout": 3})
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["status"], "ok")
        self.assertEqual(data["response"], "echo:hello")

    def test_async_task_lifecycle(self):
        resp = self._post_json("/async", {"prompt": "hi", "timeout": 3})
        data = json.loads(resp.read().decode("utf-8"))
        self.assertEqual(data["status"], "accepted")
        tid = data["task_id"]

        deadline = time.time() + 3
        last = None
        while time.time() < deadline:
            r = urllib.request.urlopen(self._url(f"/task/{tid}"), timeout=3)
            last = json.loads(r.read().decode("utf-8"))
            if last.get("status") in ("ok", "error"):
                break
            time.sleep(0.05)
        self.assertIsNotNone(last)
        self.assertEqual(last.get("status"), "ok")

