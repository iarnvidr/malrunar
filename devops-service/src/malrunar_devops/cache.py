"""Account-scoped, durable read snapshots. Credentials never enter this store."""
import hashlib
import json
import os
import time
from datetime import datetime, timezone
from pathlib import Path
from uuid import uuid4
import fcntl

MAX_STALE = 7 * 24 * 60 * 60


def stamp(value):
    return datetime.fromtimestamp(value, timezone.utc).isoformat()


class DiskCache:
    def __init__(self, root, namespace, clock=time.time):
        self.root = Path(root) / namespace
        self.root.mkdir(parents=True, exist_ok=True, mode=0o700)
        self.root.chmod(0o700)
        self.clock = clock

    def read(self, path):
        try:
            return json.loads(path.read_text())
        except (OSError, ValueError):
            return None

    def write(self, path, value):
        temporary = path.with_name(path.name + "." + uuid4().hex + ".tmp")
        try:
            with open(temporary, "x", opener=lambda p, flags: os.open(p, flags, 0o600)) as handle:
                json.dump(value, handle)
                handle.flush()
                os.fsync(handle.fileno())
            os.replace(temporary, path)
        finally:
            temporary.unlink(missing_ok=True)

    def generation(self):
        return self.read(self.root / "generation.json") or "initial"

    def invalidate(self):
        # Keep the previous snapshot for offline use, but expire every data query.
        self.write(self.root / "generation.json", uuid4().hex)

    def fetch(self, key, loader, ttl=60, force=False, workflow=False):
        digest = hashlib.sha256(json.dumps([2, key], sort_keys=True).encode()).hexdigest()
        path = self.root / (digest + ".json")
        with open(self.root / (digest + ".lock"), "a", opener=lambda p, flags: os.open(p, flags, 0o600)) as lock:
            fcntl.flock(lock, fcntl.LOCK_EX)
            previous = self.read(path)
            generation = "workflow" if workflow else self.generation()
            now = self.clock()
            valid = isinstance(previous, dict) and previous.get("version") == 2 and isinstance(previous.get("savedAt"), (int, float)) and isinstance(previous.get("data"), dict)
            fresh = valid and previous.get("generation") == generation and 0 <= now - previous["savedAt"] < ttl
            if fresh and not force:
                return self.result(previous, ttl, cached=True)
            try:
                data = loader()
            except Exception:
                if valid and 0 <= now - previous["savedAt"] <= MAX_STALE:
                    return self.result(previous, ttl, cached=True, stale=True,
                        warning="Azure DevOps could not be refreshed. Showing the last local snapshot; changes still require a live connection.")
                raise
            entry = {"version": 2, "savedAt": self.clock(), "generation": generation, "data": data}
            try:
                self.write(path, entry)
                # Bound disk usage without touching other account namespaces.
                snapshots = sorted(self.root.glob("*.json"), key=lambda p: p.stat().st_mtime, reverse=True)
                for old in snapshots[501:]:
                    if old.name != "generation.json":
                        old.unlink(missing_ok=True)
            except OSError:
                return self.result(entry, ttl, warning="The local DevOps cache could not be saved.")
            return self.result(entry, ttl)

    @staticmethod
    def result(entry, ttl, cached=False, stale=False, warning=None):
        return {**entry["data"], "cache": {"savedAt": stamp(entry["savedAt"]), "freshUntil": stamp(entry["savedAt"] + ttl),
            "fromCache": cached, "stale": stale, **({"warning": warning} if warning else {})}}


def cached_read(cache, key, loader, ttl=60, force=False):
    if cache:
        return cache.fetch(key, loader, ttl=ttl, force=force)
    now = time.time()
    return DiskCache.result({"data": loader(), "savedAt": now}, ttl,
        warning="Local caching is unavailable because the Azure account or cache directory could not be identified.")
