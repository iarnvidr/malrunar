# SPDX-License-Identifier: Apache-2.0
import json
import sys
import tempfile
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
if str(ROOT) not in sys.path:
    sys.path.insert(0, str(ROOT))

from malrunar_obs.config import load_config  # noqa: E402

CAMERA_URL = "https://camera.example.test/view?room=demo&quality=2"


def write_config(directory: Path, **sections) -> Path:
    """A JSON config with a camera URL, storage inside ``directory`` and output autostart off."""
    data = {
        "cameras": [{"url": CAMERA_URL}],
        "storage": {"recordings_dir": str(directory / "recordings"), "state_dir": str(directory / "state")},
        "obs": {"config_dir": str(directory / "obs-config")},
        "output": {"autostart": False},
    }
    for name, value in sections.items():
        if isinstance(value, dict) and isinstance(data.get(name), dict):
            data[name].update(value)
        else:
            data[name] = value
    path = directory / "config.json"
    path.write_text(json.dumps(data), encoding="utf-8")
    return path


def make_config(directory: Path, env: dict | None = None, **sections):
    path = write_config(directory, **sections)
    return load_config(env=env or {}, path=path)


class TempDir:
    """Mixin: ``self.tmp`` is a fresh Path per test."""

    def setUp(self):
        super().setUp()
        self._tmp = tempfile.TemporaryDirectory()
        self.addCleanup(self._tmp.cleanup)
        self.tmp = Path(self._tmp.name)
