# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""linuxmuster-radius control plane package.

Manages the FreeRADIUS WLAN data plane (one FreeRADIUS container per
linuxmuster server, SSID branching handled inside it) through the docker-py
SDK and exposes them via a FastAPI REST API with a thin Typer CLI client.

The version is not spelled out here: ``importlib.metadata.version("lmnradius")``
returns the one debian/changelog defines (fed in by setup.py at build time).
"""
