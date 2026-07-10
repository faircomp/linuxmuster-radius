<!--
SPDX-FileCopyrightText: Kevin Stenzel
SPDX-License-Identifier: GPL-3.0-or-later
-->

# Instance definitions (declarative)

One file = **one FreeRADIUS instance** — exactly one self-contained deployment
per linuxmuster server (ADR-002). The instance serves multiple SSIDs; each SSID
is gated in the inner tunnel by an AD group (ADR-007) and may carry a dynamic
VLAN (ADR-008). File name convention: `<name>.yaml`.

> **These files are EXAMPLES.** The control plane owns the real records: it
> writes, validates and git-commits them under
> `/var/lib/linuxmuster-radius/instances/` via the REST API / `lmnradius` CLI
> (see `controlplane/lmnradius/store.py`). Do **not** hand-edit the live files —
> use the API so validation, rendering and reconciliation stay in sync. This
> directory is only a documented sample of the shape.

## Fields

Each YAML deserialises into `Instance` (`controlplane/lmnradius/models.py`); every
field is strictly validated at the API boundary (a lax field is a path-traversal
or config-injection sink), so the examples double as the validation contract.

| Field | Meaning |
|---|---|
| `name` | Instance id → filename + container/volume name. `^[A-Za-z0-9][A-Za-z0-9-]{0,30}$`, no `/`/`..`. |
| `realm` | Kerberos realm, UPPERCASE dotted (`LINUXMUSTER.MEINESCHULE.DE`). |
| `workgroup` | NetBIOS short domain, UPPERCASE (`LINUXMUSTER`). |
| `server_fqdn` | RADIUS host FQDN == container hostname == EAP cert CN. |
| `ldap_server` | AD/LDAP URI, `ldap://`\|`ldaps://` + host + optional `:port`. |
| `ldap_base_dn` | LDAP search base DN. |
| `ldap_bind_dn` | Service-account DN used for the group lookup. |
| `wifi_group` | AD group whose members may use WLAN at all (default `wifi`). |
| `client_subnets` | List of AP-management CIDRs allowed in `clients.conf` (>=1, ADR-009). |
| `ssids` | List of `{ name, allowed_group, vlan? }` (>=1). `name` matches `&Called-Station-SSID`; `allowed_group` is the AD group gate; `vlan` (1-4094, optional) is the RFC-3580 dynamic VLAN. |
| `join_secret` | **Filename** (basename under `secrets_dir`) of the domain-join secret. |
| `ldap_bind_secret` | **Filename** of the LDAP bind password secret. |
| `radius_secret` | **Filename** of the per-subnet RADIUS shared secret. |
| `image` | Data-plane image; carries an explicit `:tag` or `@sha256:<digest>` — **digest-pinned** in production (Renovate, P5). A bare repo is rejected. |

The three `*_secret` fields are **secret file names**, never the secret values —
the values live in `/etc/linuxmuster-radius/secrets/` (0700) and are mounted into
the container. The `container_name` (`lmnradius-<name>`) is computed, not stored.
