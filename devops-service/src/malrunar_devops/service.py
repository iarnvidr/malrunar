"""Authenticated DevOps RPC engine. Provider and topic policy are explicit consumer inputs."""
import hashlib
import hmac
import json
import re
import threading
import time
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from uuid import UUID
from .errors import BoardError
from .cache import cached_read

FIELDS = {
    "board": {"scope", "type", "cursor", "linkedIds", "force"}, "item": {"id", "force"},
    "transition": {"id", "revision", "state"}, "create": {"type", "title", "description"},
    "ensure-link": {"id", "url"}, "linked-report": {"ids", "force"}, "wiki-list": {"force"},
    "wiki-tree": {"wikiId", "force"}, "wiki-page": {"wikiId", "path", "force"}, "wiki-attachment": {"wikiId", "name"},
    "wiki-publish": {"wikiId", "path", "content", "etag"}, "topics": set(), "backlink": {"topicId", "id"}, "status": set(),
    "paseo-overview": set(), "paseo-link": {"topicId", "id", "expectedRevision"},
    "paseo-create": {"topicId", "type", "title", "description", "expectedRevision"},
    "paseo-wiki-link": {"topicId", "wikiId", "path", "expectedRevision"},
}


def validate(action, payload):
    if action not in FIELDS or not isinstance(payload, dict) or set(payload) - FIELDS[action]:
        raise ValueError("Unknown action or invalid fields.")
    required = {"board": ["scope"], "item": ["id"], "transition": ["id", "revision", "state"],
        "create": ["type", "title", "description"], "ensure-link": ["id", "url"], "linked-report": ["ids"],
        "wiki-tree": ["wikiId"], "wiki-page": ["wikiId", "path"], "wiki-publish": ["wikiId", "path", "content", "etag"], "backlink": ["topicId", "id"],
        "paseo-link": ["topicId", "id", "expectedRevision"], "paseo-create": ["topicId", "type", "title", "description", "expectedRevision"],
        "paseo-wiki-link": ["topicId", "wikiId", "path", "expectedRevision"], "wiki-attachment": ["wikiId", "name"]}
    if any(k not in payload for k in required.get(action, [])):
        raise ValueError("Required fields are missing.")
    for key, value in payload.items():
        if key in ("id", "revision", "cursor") and (type(value) is not int or not 0 < value < 2**31):
            raise ValueError("Invalid ID or revision.")
        if key in ("ids", "linkedIds") and (not isinstance(value, list) or len(value) > (100 if key == "ids" else 1000) or any(type(i) is not int or not 0 < i < 2**31 for i in value)):
            raise ValueError("Invalid work item IDs.")
        if key == "force" and type(value) is not bool:
            raise ValueError("Invalid refresh option.")
        limits = {"type": 120, "title": 255, "description": 12000, "state": 120, "url": 2048, "topicId": 200, "path": 1000, "content": 250000, "etag": 200}
        if key in limits and (not isinstance(value, str) or len(value) > limits[key] or (key not in ("description", "etag") and not value.strip())):
            raise ValueError("Invalid or oversized text.")
        if key == "wikiId":
            UUID(value)
        if key == "name" and (not isinstance(value, str) or not 0 < len(value) <= 500 or value in (".", "..") or any(c in value for c in ("/", "\\")) or any(ord(c) < 32 for c in value)):
            raise ValueError("Invalid attachment name.")
        if key == "expectedRevision" and (not isinstance(value, str) or not re.fullmatch(r"[a-f0-9]{64}", value)):
            raise ValueError("Refresh the local Paseo index before linking.")
    if action == "board" and payload["scope"] not in ("all-open", "mine", "linked"):
        raise ValueError("Invalid work item scope.")
    # Linked IDs do not alter the identity of an all-open/mine snapshot.
    return {k: v for k, v in payload.items() if not (action == "board" and k == "linkedIds" and payload["scope"] != "linked")}


class Backend:
    def __init__(self, *, runner=None, cache_factory=None, topic_reader=None,
                 paseo_runner=None, linked_reader=None, authorize=None,
                 refresh_seconds=300, error_type=BoardError,
                 service_name="devops", cache_description="account-scoped"):
        # There is deliberately no permissive policy or provider fallback.
        callbacks = (runner, cache_factory, topic_reader, paseo_runner, linked_reader, authorize)
        if any(not callable(callback) for callback in callbacks):
            raise ValueError("Explicit provider callbacks and authorization policy are required.")
        if not isinstance(error_type, type) or not issubclass(error_type, Exception):
            raise ValueError("Invalid provider error type.")
        self.runner, self.cache_factory, self.topic_reader = runner, cache_factory, topic_reader
        self.paseo_runner, self.linked_reader, self.authorize = paseo_runner, linked_reader, authorize
        self.error_type = error_type
        self.service_name, self.cache_description = service_name, cache_description
        self.lock = threading.Lock()
        self.status = {"lastRefreshAt": None, "lastSuccessAt": None, "warning": "",
                       "refreshSeconds": max(60, int(refresh_seconds))}

    def execute(self, action, payload, request_id=None):
        payload = validate(action, payload)
        if self.authorize(action, payload) is not True:
            raise self.error_type("The configured policy denied this operation.")
        if action.startswith("paseo-"):
            if action == "paseo-create":
                if not isinstance(request_id, str) or not re.fullmatch(r"[a-zA-Z0-9-]{8,100}", request_id):
                    raise ValueError("A unique operation identifier is required.")
                payload = {**payload, "operationId": request_id}
            return self.paseo_runner(action, payload)
        if action == "board" and payload["scope"] == "linked":
            if "linkedIds" not in payload:
                payload["linkedIds"] = [i for t in self.topic_reader()["topics"] for i in t["workItemIds"]]
            payload["linkedIds"] = sorted(set(payload["linkedIds"]))
        if action == "status":
            return {**self.status, "service": self.service_name, "cache": self.cache_description}
        if action == "topics":
            return self.topic_reader()
        if action == "backlink":
            topic = next((t for t in self.topic_reader()["topics"] if t["id"] == payload["topicId"]), None)
            if not topic or payload["id"] not in topic["workItemIds"]:
                raise ValueError("This item is not linked to that public Paseo topic.")
            return self.runner("ensure-link", {"id": payload["id"], "url": topic["url"]}, self.cache_factory())
        if action in ("create", "wiki-publish"):
            if not isinstance(request_id, str) or not re.fullmatch(r"[\w-]{8,100}", request_id):
                raise ValueError("A unique operation identifier is required.")
            cache = self.cache_factory()
            if not cache:
                raise self.error_type("The Azure account could not be identified. Sign in before publishing.")
            directory = cache.root / "operations"
            directory.mkdir(mode=0o700, exist_ok=True)
            path = directory / (request_id + ".json")
            signature = hashlib.sha256(json.dumps([action, payload], sort_keys=True).encode()).hexdigest()
            with self.lock:
                if path.exists():
                    record = json.loads(path.read_text())
                    if record.get("signature") != signature or "result" not in record:
                        raise self.error_type("The previous publication has an uncertain result. Check DevOps before creating again.")
                    return record["result"]
                cache.write(path, {"signature": signature, "pending": True})
            result = self.runner(action, payload, cache)
            cache.write(path, {"signature": signature, "result": result})
            return result
        if action == "linked-report":
            return cached_read(self.cache_factory(), {"action": action, "ids": sorted(payload["ids"])}, lambda: self.linked_reader(payload["ids"]), ttl=300, force=payload.get("force", False))
        return self.runner(action, payload, self.cache_factory())

    def refresh(self):
        self.status["lastRefreshAt"] = time.time()
        try:
            cursor = None
            for _ in range(100):
                result = self.execute("board", {"scope": "all-open", **({"cursor": cursor} if cursor else {})})
                if result.get("cache", {}).get("stale"):
                    raise self.error_type("DevOps refresh failed. The saved snapshot was retained.")
                cursor = result.get("nextCursor")
                if not cursor:
                    break
            self.status.update(lastSuccessAt=time.time(), warning="")
        except Exception:
            self.status["warning"] = "DevOps cannot refresh right now. The last saved snapshot was retained."


def create_server(address, token, backend):
    if len(token) < 32:
        raise ValueError("DevOps service token is missing")
    class Handler(BaseHTTPRequestHandler):
        def log_message(self, *_):
            pass  # Request bodies, credentials and Azure errors never enter access logs.

        def reply(self, status, value):
            body = json.dumps(value).encode()
            self.send_response(status)
            for key, value in {"Content-Type": "application/json; charset=utf-8", "Cache-Control": "no-store", "Content-Length": str(len(body)), "X-Content-Type-Options": "nosniff"}.items():
                self.send_header(key, value)
            self.end_headers()
            self.wfile.write(body)

        def do_GET(self):
            self.reply(200 if self.path == "/health" else 404, {"service": "devops", "ready": True} if self.path == "/health" else {"error": "Not found"})

        def do_POST(self):
            if not hmac.compare_digest(self.headers.get("Authorization", "").encode(), ("Bearer " + token).encode()):
                return self.reply(401, {"error": "Unauthorized"})
            if self.path != "/rpc":
                return self.reply(404, {"error": "Not found"})
            try:
                length = int(self.headers.get("Content-Length", "0"))
                if not 0 < length <= 1024 * 1024:
                    return self.reply(413, {"error": "Request is too large"})
                body = json.loads(self.rfile.read(length))
                result = backend.execute(body["action"], body.get("payload", {}), body.get("requestId"))
                self.reply(200, {"result": result})
            except (ValueError, KeyError, TypeError):
                self.reply(400, {"error": "Invalid DevOps request."})
            except (BoardError, backend.error_type) as error:
                self.reply(409, {"error": str(error)})
            except Exception:
                self.reply(503, {"error": "DevOps is unavailable. Check the Azure login and connection. Check DevOps before repeating a publication."})
    server = ThreadingHTTPServer(address, Handler)
    server.daemon_threads = True
    return server
