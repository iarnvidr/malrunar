# SPDX-License-Identifier: Apache-2.0
"""Generate the OBS configuration from the obs-control config (run before OBS starts).

``python3 -m malrunar_obs.setup``              write the profile, scene collection, websocket settings
``python3 -m malrunar_obs.setup --print-args``  also print the obs arguments, NUL separated
``python3 -m malrunar_obs.setup --check``       only validate the configuration and the plugins
``python3 -m malrunar_obs.setup --print-config-dir``  the OBS config directory (must be named obs-studio)
"""
from __future__ import annotations

import sys
from pathlib import Path

from .config import ConfigError, load_config
from .plugins import PluginError, load_plugins
from .scenes import generate, launch_args


def main(argv: list[str]) -> int:
    try:
        config = load_config()
        plugins = load_plugins(config)
        if "--print-config-dir" in argv:
            if Path(config.obs.config_dir).name != "obs-studio":
                raise ValueError(f"[obs] config_dir must end in /obs-studio (got {config.obs.config_dir})")
            print(config.obs.config_dir)
            return 0
        if "--check" in argv:
            from .scenes import build_collection

            build_collection(config, plugins)
            return 0
        generate(config, plugins)
    except (ConfigError, PluginError, ValueError) as exc:
        print(f"obs-control configuration error: {exc}", file=sys.stderr)
        return 2
    if "--print-args" in argv:
        sys.stdout.write("".join(arg + "\0" for arg in launch_args(config)))
    return 0


if __name__ == "__main__":
    sys.exit(main(sys.argv[1:]))
