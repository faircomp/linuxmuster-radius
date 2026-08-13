# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Runtime settings for the control plane.

Settings come from an optional YAML file, overlaid by ``LMNRADIUS_*``
environment variables (environment always wins over the file).
"""

from __future__ import annotations

import logging
import os
from pathlib import Path
from typing import Any

import yaml
from pydantic_settings import BaseSettings, SettingsConfigDict

_ENV_PREFIX = "LMNRADIUS_"


class Settings(BaseSettings):
    """Control-plane configuration."""

    api_token: str
    instances_dir: str = "/var/lib/linuxmuster-radius/instances"
    secrets_dir: str = "/etc/linuxmuster-radius/secrets"
    certs_dir: str = "/etc/linuxmuster-radius/certs"
    render_dir: str = "/var/lib/linuxmuster-radius/instance.d"
    docker_host: str | None = None
    bind_host: str = "127.0.0.1"
    bind_port: int = 8080
    api_url: str = "http://127.0.0.1:8080"
    container_bind_ip: str = "0.0.0.0"  # host IP the RADIUS ports bind to; 0.0.0.0 = all
    # interfaces so the APs (the NAS) can reach 1812/1813; access is gated by the
    # per-client shared secret + EAP. Narrow to a specific IP if the host has an
    # untrusted interface.
    log_max_size: str = "20m"  # docker json-log cap per container (live view)
    log_max_file: int = 5
    version: str = "0.1.6"

    model_config = SettingsConfigDict(env_prefix=_ENV_PREFIX)


def load_settings(config_path: str | None = None) -> Settings:
    """Load :class:`Settings` from an optional YAML file plus environment.

    Values read from ``config_path`` act as defaults; any matching
    ``LMNRADIUS_<FIELD>`` environment variable takes precedence.
    """
    if config_path is None:
        config_path = os.environ.get("LMNRADIUS_CONFIG", "/etc/linuxmuster-radius/config.yml")
    overrides: dict[str, Any] = {}
    if config_path:
        path = Path(config_path)
        if path.is_file():
            if path.stat().st_mode & 0o077:
                logging.getLogger("lmnradius").warning(
                    "config %s is group/world accessible (mode %o); it holds the API token — chmod 600",
                    config_path,
                    path.stat().st_mode & 0o777,
                )
            data = yaml.safe_load(path.read_text()) or {}
            if isinstance(data, dict):
                for key, value in data.items():
                    field = str(key)
                    env_name = f"{_ENV_PREFIX}{field.upper()}"
                    # Let the environment override the file.
                    if env_name in os.environ:
                        continue
                    overrides[field] = value
    return Settings(**overrides)
