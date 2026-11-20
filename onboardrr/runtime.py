"""Keep system tools independent of native bundle libraries."""

import os
import sys
import tomllib
from pathlib import Path


CONTROLLER_SETTINGS = {
    "openbao_url": "ONBOARDRR_BAO_URL",
    "openbao_mount": "ONBOARDRR_BAO_MOUNT",
    "openbao_layout": "ONBOARDRR_BAO_LAYOUT",
    "openbao_token_file": "ONBOARDRR_BAO_TOKEN_FILE",
    "periphery_retire_token_file": "ONBOARDRR_BAO_PERIPHERY_RETIRE_TOKEN_FILE",
    "komodo_url": "ONBOARDRR_KOMODO_URL",
    "komodo_openbao_token_file": "ONBOARDRR_KOMODO_BAO_TOKEN_FILE",
    "newt_openbao_mount": "ONBOARDRR_NEWT_BAO_MOUNT",
    "pangolin_url": "ONBOARDRR_PANGOLIN_URL",
    "pangolin_ssh_target": "ONBOARDRR_PANGOLIN_SSH_TARGET",
    "goku_host": "ONBOARDRR_GOKU_HOST",
    "goku_mount_host": "ONBOARDRR_GOKU_MOUNT_HOST",
    "monitoring_host": "ONBOARDRR_MONITORING_HOST",
    "monitoring_stack": "ONBOARDRR_MONITORING_STACK",
    "beszel_url": "ONBOARDRR_BESZEL_URL",
    "wud_url": "ONBOARDRR_WUD_URL",
    "termix_url": "ONBOARDRR_TERMIX_URL",
}


def load_controller_settings():
    config_home = Path(os.environ.get("XDG_CONFIG_HOME", ""))
    if not config_home.is_absolute():
        config_home = Path.home() / ".config"
    path = config_home / "onboardrr" / "config.toml"
    try:
        with path.open("rb") as stream:
            settings = tomllib.load(stream)
    except FileNotFoundError:
        return
    except (UnicodeError, tomllib.TOMLDecodeError) as error:
        raise ValueError(f"Use valid TOML in {path}.") from error
    for key in CONTROLLER_SETTINGS:
        if key in settings and not (isinstance(settings[key], str) and settings[key].strip()):
            raise ValueError(f"Set {key} to a nonempty string in {path}.")
    for key, variable in CONTROLLER_SETTINGS.items():
        if key in settings:
            os.environ.setdefault(variable, settings[key].strip())


def system_environment():
    environment = os.environ.copy()
    if getattr(sys, "frozen", False) and sys.platform.startswith("linux"):
        original = environment.pop("LD_LIBRARY_PATH_ORIG", None)
        if original is None:
            environment.pop("LD_LIBRARY_PATH", None)
        else:
            environment["LD_LIBRARY_PATH"] = original
    return environment
