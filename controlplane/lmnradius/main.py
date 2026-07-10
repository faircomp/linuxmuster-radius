# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Console entrypoint that assembles and serves the control plane API."""

from __future__ import annotations

import logging

import uvicorn

from .api import create_app
from .config import load_settings
from .docker_service import DockerService
from .reconciler import Reconciler
from .store import Store
from .updater import Updater


def _warn_if_insecure_bind(bind_host: str) -> None:
    """Warn if the API is bound off-loopback — the token would travel in cleartext.

    In-app TLS is intentionally not implemented; off-host access must go through an
    operator-managed TLS reverse proxy (see docs/architecture.md, threat-model T5).
    """
    if bind_host not in ("127.0.0.1", "localhost", "::1"):
        logging.getLogger("lmnradius").warning(
            "API is bound to non-loopback %s over plain HTTP: the bearer token "
            "(root-equivalent) travels in cleartext. Bind 127.0.0.1, or put an "
            "operator-managed TLS reverse proxy in front — in-app TLS is not implemented.",
            bind_host,
        )


def main() -> None:
    """Load settings, wire the components together and run the uvicorn server."""
    logging.basicConfig(level=logging.INFO)
    settings = load_settings()
    store = Store(settings.instances_dir)
    docker = DockerService(
        docker_host=settings.docker_host,
        secrets_dir=settings.secrets_dir,
        certs_dir=settings.certs_dir,
        render_dir=settings.render_dir,
        container_bind_ip=settings.container_bind_ip,
        log_max_size=settings.log_max_size,
        log_max_file=settings.log_max_file,
    )
    reconciler = Reconciler(store, docker)
    updater = Updater(store, docker, reconciler)
    app = create_app(settings, store, reconciler, docker, updater)
    _warn_if_insecure_bind(settings.bind_host)
    uvicorn.run(app, host=settings.bind_host, port=settings.bind_port)


if __name__ == "__main__":
    main()
