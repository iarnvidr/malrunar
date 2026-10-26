"""Explicit consumer adapter entry point; no deployment or provider defaults."""
import importlib.util
import os
from pathlib import Path
import stat
import sys
import threading
import time

from .service import Backend, create_server


def load_backend(path):
    if not path or not Path(path).is_absolute() or not Path(path).is_file():
        raise ValueError("MALRUNAR_DEVOPS_ADAPTER must name an absolute adapter file.")
    spec = importlib.util.spec_from_file_location("malrunar_devops_consumer", path)
    module = importlib.util.module_from_spec(spec)
    spec.loader.exec_module(module)
    backend = module.create_backend()
    if not isinstance(backend, Backend):
        raise ValueError("The adapter must return a configured Backend.")
    return backend


def read_token(path):
    if not path or not Path(path).is_absolute():
        raise ValueError("MALRUNAR_DEVOPS_TOKEN_FILE is required.")
    with open(path, encoding="utf-8") as stream:
        info = os.fstat(stream.fileno())
        if not stat.S_ISREG(info.st_mode) or info.st_uid != os.getuid() or info.st_mode & 0o077:
            raise ValueError("The service token must be an owner-only regular file.")
        token = stream.read(4097).strip()
    if not 32 <= len(token) <= 4096:
        raise ValueError("Invalid service token length.")
    return token


def main():
    try:
        # Validate explicit secret custody before executing the trusted private adapter.
        token = read_token(os.environ.get("MALRUNAR_DEVOPS_TOKEN_FILE"))
        backend = load_backend(os.environ.get("MALRUNAR_DEVOPS_ADAPTER"))
        server = create_server((os.environ.get("MALRUNAR_DEVOPS_LISTEN", "127.0.0.1"),
                                int(os.environ.get("MALRUNAR_DEVOPS_PORT", "18392"))), token, backend)
    except Exception:
        # Never emit adapter exceptions, credential paths or provider responses.
        print("DevOps configuration unavailable; explicit adapter and owner token required.", file=sys.stderr)
        return 78

    def poll():
        while True:
            backend.refresh()
            time.sleep(backend.status["refreshSeconds"])

    threading.Thread(target=poll, daemon=True).start()
    server.serve_forever()
    return 0


if __name__ == "__main__":
    sys.exit(main())
