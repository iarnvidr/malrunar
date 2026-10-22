# SPDX-License-Identifier: Apache-2.0
"""Plugin loading. A plugin is a Python module, trusted exactly like the rest of the container.

Configured with ``[plugins] modules`` (``MALRUNAR_OBS_PLUGINS``, comma separated) and
``[plugins] dir`` (``MALRUNAR_OBS_PLUGINS_DIR``). Each entry is either an importable module name
or a path to a ``.py`` file; every ``*.py`` file in the directory (names not starting with ``_``) is
loaded in name order, and the directory is put on ``sys.path`` for the plugin's own imports.

A plugin may define any of these functions (all optional):

``extend_collection(builder, config)``
    Called while the scene collection is generated, before OBS starts. Add scenes and sources with
    ``builder.add_source`` / ``builder.add_scene`` and profile settings with ``builder.profile_ini``
    and ``builder.profile_files`` (see ``scenes.CollectionBuilder``).

``register(controller)``
    Called once when the controller is built. ``controller`` is a ``controller.Controller``: add
    routes to ``controller.app.router``, startup/cleanup hooks to ``controller.app.on_startup`` /
    ``on_cleanup``, use ``controller.obs(...)`` for obs-websocket requests, claim the program with
    ``controller.add_program_holder(...)`` and extend ``/ready`` with
    ``controller.add_ready_info(...)``. See docs/obs-control-api.md.
"""
from __future__ import annotations

import importlib
import importlib.util
import sys
from pathlib import Path
from types import ModuleType
from typing import Iterable

from .config import Config


class PluginError(RuntimeError):
    pass


def _load_file(path: Path) -> ModuleType:
    name = f"malrunar_obs_plugin_{path.stem}"
    spec = importlib.util.spec_from_file_location(name, path)
    if spec is None or spec.loader is None:
        raise PluginError(f"cannot load plugin file {path}")
    module = importlib.util.module_from_spec(spec)
    sys.modules[name] = module
    try:
        spec.loader.exec_module(module)
    except Exception as exc:
        del sys.modules[name]
        raise PluginError(f"plugin {path} failed to load: {exc}") from exc
    return module


def load_plugins(config: Config) -> list[ModuleType]:
    modules: list[ModuleType] = []
    directory = config.plugins.dir
    if directory:
        folder = Path(directory)
        if not folder.is_dir():
            raise PluginError(f"plugins dir {directory} does not exist")
        if str(folder) not in sys.path:
            sys.path.insert(0, str(folder))
        for path in sorted(folder.glob("*.py")):
            if not path.name.startswith("_"):
                modules.append(_load_file(path))
    for entry in config.plugins.modules:
        if entry.endswith(".py") or "/" in entry:
            path = Path(entry)
            if not path.is_file():
                raise PluginError(f"plugin file {entry} does not exist")
            modules.append(_load_file(path))
        else:
            try:
                modules.append(importlib.import_module(entry))
            except Exception as exc:
                raise PluginError(f"plugin module {entry} failed to import: {exc}") from exc
    return modules


def call_hook(plugins: Iterable[ModuleType], hook: str, *args: object) -> None:
    for plugin in plugins:
        function = getattr(plugin, hook, None)
        if function is not None:
            function(*args)
