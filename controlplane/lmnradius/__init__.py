# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""linuxmuster-radius control plane package.

Manages the FreeRADIUS WLAN data plane (one FreeRADIUS container per
linuxmuster server, SSID branching handled inside it) through the docker-py
SDK and exposes them via a FastAPI REST API with a thin Typer CLI client.
"""

from __future__ import annotations

__version__ = "0.1.4"

__all__ = ["__version__"]
