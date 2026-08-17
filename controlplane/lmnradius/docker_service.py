# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""Docker orchestration for linuxmuster-radius instances via the docker-py SDK.

One FreeRADIUS container per instance, named ``lmnradius-<name>``. Unlike the
stateless squid sibling this container is a *stateful* AD member: it keeps its
machine-account secret in a persistent ``/var/lib/samba`` volume and runs two
daemons (radiusd + winbindd).

Config split (docs/architecture.md §5): scalar fields go in as an env whitelist
that ``image/entrypoint.sh`` renders via ``envsubst``; the two *list* fields
(``client_subnets`` -> ``clients.conf``, ``ssids`` -> ``ssid-policy``) cannot be
expressed with envsubst, so the control plane renders them here into
``render_dir/<name>/`` and bind-mounts that directory read-only at
``/etc/lmnradius/instance.d`` (the entrypoint's ``MOUNT_D``). Secrets and EAP
cert material are bind-mounted read-only as files; only their *paths* travel in
the env.
"""

from __future__ import annotations

import os
from pathlib import Path
from typing import Any, Optional

import docker
from docker.errors import ImageNotFound, NotFound
from docker.models.containers import Container
from docker.types import LogConfig

from . import diagnostics
from .models import Instance
from .render import render_clients_conf, render_ssid_policy

# -- container-side mount points consumed by image/entrypoint.sh ---------------
_INSTANCE_D_MOUNT = "/etc/lmnradius/instance.d"  # entrypoint MOUNT_D (clients.conf + ssid-policy)
_SECRETS_MOUNT = "/run/secrets"  # ro-mounted secret FILES (paths land in the env)
_CERTS_MOUNT = "/run/secrets/eap"  # ro-mounted EAP cert material (matches entrypoint EAP_* paths)
_SAMBA_STATE = "/var/lib/samba"  # entrypoint STATEDIR persistent volume

# EAP cert material filenames inside ``certs_dir/<name>/`` (control-plane cert output).
_CA_FILE = "ca.pem"
_CERT_FILE = "server.pem"
_KEY_FILE = "server.key"

# Rendered per-instance config filenames — MUST match the entrypoint's MOUNT_D reads.
_CLIENTS_FILE = "clients.conf"
_SSID_POLICY_FILE = "ssid-policy"

# FreeRADIUS auth + accounting ports (RFC 2865/2866); fixed in the image.
_AUTH_PORT = 1812
_ACCT_PORT = 1813


class DockerService:
    """Manage one FreeRADIUS container per instance through the Docker Engine API.

    A container's real name is ``lmnradius-<name>`` where ``<name>`` is the
    instance's :pyattr:`Instance.name`.
    """

    def __init__(
        self,
        docker_host: Optional[str] = None,
        secrets_dir: str = "/etc/linuxmuster-radius/secrets",
        certs_dir: str = "/etc/linuxmuster-radius/certs",
        render_dir: str = "/var/lib/linuxmuster-radius/instance.d",
        container_bind_ip: str = "0.0.0.0",
        log_max_size: str = "20m",
        log_max_file: int = 5,
    ) -> None:
        self.docker_host: Optional[str] = docker_host
        self.secrets_dir: str = secrets_dir
        self.certs_dir: str = certs_dir
        self.render_dir: str = render_dir
        self.container_bind_ip: str = container_bind_ip
        self.log_max_size: str = log_max_size
        self.log_max_file: int = log_max_file
        self.client: docker.DockerClient = (
            docker.DockerClient(base_url=docker_host) if docker_host else docker.from_env()
        )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _container_name(name: str) -> str:
        return f"lmnradius-{name}"

    def _get(self, name: str) -> Optional[Container]:
        """Return the container for ``name`` or ``None`` if it does not exist."""
        try:
            return self.client.containers.get(self._container_name(name))
        except NotFound:
            return None

    def _pull(self, image: str) -> None:
        """Pull ``image`` best-effort, handling a ``@sha256:`` digest pin and a
        ``:tag`` (without mistaking a registry ``host:port`` for a tag)."""
        if "@" in image:
            # Digest pin ``repo@sha256:<hex>``: keep the whole ``sha256:<hex>`` as the
            # tag so docker-py pulls the digest (a plain ``rsplit(':')`` would drop the
            # ``sha256:`` prefix and pull a non-existent tag).
            repository, _, digest = image.partition("@")
            self.client.images.pull(repository, tag=digest)
            return
        repository = image
        tag: Optional[str] = None
        # Only treat a colon in the final path segment as a tag separator so we
        # do not mistake a registry ``host:port`` for a tag.
        last_segment = image.rsplit("/", 1)[-1]
        if ":" in last_segment:
            repository, tag = image.rsplit(":", 1)
        if tag is not None:
            self.client.images.pull(repository, tag=tag)
        else:
            self.client.images.pull(repository)

    def _resolve_under(self, root: str, child: str) -> str:
        """Resolve ``root/child`` and assert it stays inside ``root``.

        Defence in depth: the model already forbids ``/`` and ``..`` in the
        instance name and the secret filenames, but every bind-mount source is
        re-checked here before it reaches the Docker API."""
        root_real = os.path.realpath(root)
        target = os.path.realpath(os.path.join(root_real, child))
        if os.path.commonpath([root_real, target]) != root_real:
            raise ValueError(f"path {child!r} escapes {root!r}")
        return target

    # -- environment -------------------------------------------------------

    def env_for(self, inst: Instance) -> dict[str, str]:
        """Build the env whitelist consumed by image/entrypoint.sh.

        Every key here is read by the entrypoint. The ``*_SECRET`` and ``EAP_*``
        values are the *container* paths of the read-only file mounts set up in
        :meth:`ensure_running` — they hold PATHS, never secret material."""
        return {
            "INSTANCE": inst.name,
            "REALM": inst.realm,
            "WORKGROUP": inst.workgroup,
            "SERVER_FQDN": inst.server_fqdn,
            "LDAP_SERVER": inst.ldap_server,
            "LDAP_BASE_DN": inst.ldap_base_dn,
            "LDAP_BIND_DN": inst.ldap_bind_dn,
            "WIFI_GROUP": inst.wifi_group,
            "LDAP_BIND_SECRET": f"{_SECRETS_MOUNT}/{inst.ldap_bind_secret}",
            "JOIN_SECRET": f"{_SECRETS_MOUNT}/{inst.join_secret}",
            "EAP_CA": f"{_CERTS_MOUNT}/{_CA_FILE}",
            "EAP_CERT": f"{_CERTS_MOUNT}/{_CERT_FILE}",
            "EAP_KEY": f"{_CERTS_MOUNT}/{_KEY_FILE}",
        }

    # -- rendering ---------------------------------------------------------

    @staticmethod
    def _write_private(path: str, content: str) -> None:
        """Write ``content`` to ``path`` with mode 0600.

        clients.conf embeds the AP shared secret, so it must never be world/group
        readable at rest; container root reads it via the read-only mount."""
        fd = os.open(path, os.O_WRONLY | os.O_CREAT | os.O_TRUNC, 0o600)
        with os.fdopen(fd, "w", encoding="utf-8") as fh:
            fh.write(content)
        # O_CREAT's mode only applies on creation; re-assert perms for a pre-existing file.
        os.chmod(path, 0o600)

    def _render_instance(self, inst: Instance) -> str:
        """Render clients.conf + ssid-policy into ``render_dir/<name>/``; return it.

        The AP shared-secret VALUE is read from ``secrets_dir/<radius_secret>``
        (never stored in the instance YAML) and rendered into clients.conf."""
        secret_path = self._resolve_under(self.secrets_dir, inst.radius_secret)
        if not os.path.isfile(secret_path):
            raise FileNotFoundError(
                f"radius_secret file missing: {secret_path} "
                "(put the AP shared secret there, mode 0600)"
            )
        # Strip the trailing newline a secret file usually carries (matches the
        # entrypoint's ``$(cat)`` for LDAP_BIND_PW); render rejects an embedded newline.
        radius_secret = Path(secret_path).read_text(encoding="utf-8").rstrip("\r\n")

        inst_dir = self._resolve_under(self.render_dir, inst.name)
        os.makedirs(inst_dir, exist_ok=True)
        os.chmod(inst_dir, 0o700)
        self._write_private(
            os.path.join(inst_dir, _CLIENTS_FILE), render_clients_conf(inst, radius_secret)
        )
        self._write_private(os.path.join(inst_dir, _SSID_POLICY_FILE), render_ssid_policy(inst))
        return inst_dir

    # -- lifecycle ---------------------------------------------------------

    def ensure_running(self, inst: Instance) -> dict[str, Any]:
        """Idempotently (re)create and start the container for ``inst``.

        Renders the list-config, resolves and FAILS CLOSED on every mount source
        (missing EAP cert material — load-bearing for PEAP server-cert pinning —
        or a missing secret raises here, before the running container is touched),
        then removes any existing ``lmnradius-<name>`` and starts a fresh one with
        the instance env, the persistent ``/var/lib/samba`` volume, an
        ``unless-stopped`` restart policy and the hardened profile from the SPEC.
        """
        try:
            self._pull(inst.image)
        except (ImageNotFound, docker.errors.APIError):
            # Fall back to a locally available image if the pull fails.
            pass

        # Render + resolve every mount source and validate BEFORE we tear down the
        # running container, so a config/secret/cert problem never causes downtime.
        render_host = self._render_instance(inst)

        join_host = self._resolve_under(self.secrets_dir, inst.join_secret)
        ldap_bind_host = self._resolve_under(self.secrets_dir, inst.ldap_bind_secret)
        for label, path in (("join_secret", join_host), ("ldap_bind_secret", ldap_bind_host)):
            if not os.path.isfile(path):
                raise FileNotFoundError(f"{label} file missing: {path} (mount it read-only, 0600)")

        # cert-check: server-cert pinning is load-bearing (docs/threat-model / ADR-005),
        # so refuse to start an instance whose EAP CA/cert/key are not present.
        cert_dir = self._resolve_under(self.certs_dir, inst.name)
        ca_host = os.path.join(cert_dir, _CA_FILE)
        cert_host = os.path.join(cert_dir, _CERT_FILE)
        key_host = os.path.join(cert_dir, _KEY_FILE)
        for path in (ca_host, cert_host, key_host):
            if not os.path.isfile(path):
                raise FileNotFoundError(
                    f"EAP cert material missing: {path} "
                    "(run 'lmnradius cert issue' — server-cert pinning is load-bearing)"
                )

        env = self.env_for(inst)

        existing = self._get(inst.name)
        if existing is not None:
            existing.remove(force=True)

        self.client.containers.run(
            inst.image,
            name=inst.container_name,
            # entrypoint pitfall: the container hostname MUST equal SERVER_FQDN (Kerberos
            # SPN canonicalisation + AD join). Docker also adds it to /etc/hosts so the
            # forward-DNS lookup the join needs resolves inside the container.
            hostname=inst.server_fqdn,
            environment=env,
            detach=True,
            restart_policy={"Name": "unless-stopped"},
            read_only=True,
            # Read-only rootfs; the entrypoint writes only to tmpfs (/run, /tmp) and the
            # /var/lib/samba volume (docs/architecture.md §3: rootfs partially relaxed for
            # the Samba machine-account state).
            tmpfs={"/run": "", "/tmp": ""},
            cap_drop=["ALL"],
            # root copies the secrets + chowns the tmpfs config, then radiusd/winbindd drop
            # to 'freerad'. FOWNER is deliberately NOT granted (copy_secret chmods before chown).
            cap_add=["SETUID", "SETGID", "DAC_OVERRIDE", "CHOWN"],
            security_opt=["no-new-privileges:true"],
            # Docker json-log is capped (live view only); radiusd/winbindd log to stdout.
            log_config=LogConfig(
                type="json-file",
                config={"max-size": self.log_max_size, "max-file": str(self.log_max_file)},
            ),
            volumes={
                render_host: {"bind": _INSTANCE_D_MOUNT, "mode": "ro"},
                # bind targets come straight from env_for so the mount paths and the env
                # paths the entrypoint reads can never drift apart.
                join_host: {"bind": env["JOIN_SECRET"], "mode": "ro"},
                ldap_bind_host: {"bind": env["LDAP_BIND_SECRET"], "mode": "ro"},
                ca_host: {"bind": env["EAP_CA"], "mode": "ro"},
                cert_host: {"bind": env["EAP_CERT"], "mode": "ro"},
                key_host: {"bind": env["EAP_KEY"], "mode": "ro"},
                # Persistent AD machine-account secret (secrets.tdb); survives recreate so
                # the one-time domain join is not repeated on every reconcile/update.
                f"lmnradius-samba-{inst.name}": {"bind": _SAMBA_STATE, "mode": "rw"},
            },
            ports={
                f"{_AUTH_PORT}/udp": (self.container_bind_ip, _AUTH_PORT),
                f"{_ACCT_PORT}/udp": (self.container_bind_ip, _ACCT_PORT),
            },
        )
        return self.status(inst.name)

    def start(self, name: str) -> dict[str, Any]:
        container = self._get(name)
        if container is not None:
            container.start()
        return self.status(name)

    def stop(self, name: str) -> dict[str, Any]:
        container = self._get(name)
        if container is not None:
            container.stop()
        return self.status(name)

    def restart(self, name: str) -> dict[str, Any]:
        container = self._get(name)
        if container is not None:
            container.restart()
        return self.status(name)

    def remove(self, name: str) -> None:
        container = self._get(name)
        if container is not None:
            container.remove(force=True)
        # Drop the rendered per-instance config (it embeds the AP shared secret). The
        # samba volume is intentionally left so a re-add with the same name adopts the
        # existing machine account instead of re-joining.
        try:
            inst_dir = self._resolve_under(self.render_dir, name)
        except ValueError:
            return
        if os.path.isdir(inst_dir):
            for fname in (_CLIENTS_FILE, _SSID_POLICY_FILE):
                fpath = os.path.join(inst_dir, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
            try:
                os.rmdir(inst_dir)
            except OSError:
                pass

    # -- introspection -----------------------------------------------------

    def status(self, name: str) -> dict[str, Any]:
        """Return the current state of the container for ``name``."""
        container = self._get(name)
        if container is None:
            return {
                "name": name,
                "exists": False,
                "running": False,
                "health": None,
                "image": None,
            }

        container.reload()
        state: dict[str, Any] = container.attrs.get("State", {}) or {}
        running = bool(state.get("Running", False))

        health: Optional[str] = None
        health_state = state.get("Health")
        if isinstance(health_state, dict):
            status_value = health_state.get("Status")
            health = status_value if isinstance(status_value, str) else None

        image: Optional[str] = None
        image_obj = container.image
        if image_obj is not None and image_obj.tags:
            image = image_obj.tags[0]

        return {
            "name": name,
            "exists": True,
            "running": running,
            "health": health,
            "image": image,
        }

    def logs(
        self,
        name: str,
        tail: int = 100,
        since: Optional[int] = None,
        until: Optional[int] = None,
        grep: Optional[str] = None,
    ) -> str:
        """Return the last ``tail`` lines of the live docker log (radiusd + winbindd).

        ``since``/``until`` are Unix epoch seconds; ``grep`` is a plain substring filter
        applied in Python (no shell — injection-safe)."""
        container = self._get(name)
        if container is None:
            return ""
        kwargs: dict[str, Any] = {"tail": tail}
        if since is not None:
            kwargs["since"] = since
        if until is not None:
            kwargs["until"] = until
        data = container.logs(**kwargs)
        text = data.decode("utf-8", errors="replace") if isinstance(data, bytes) else str(data)
        if grep:
            text = "\n".join(line for line in text.splitlines() if grep in line)
        return text

    def _exec(self, container: Container, cmd: list[str]) -> tuple[int, str]:
        """Run ``cmd`` (argv list, no shell) in the container; return (exit, output).

        argv form means the password element is never shell-interpreted — no
        quoting/injection surface. Output is combined stdout+stderr (ntlm_auth
        writes its status to stdout, wbinfo mixes)."""
        exit_code, out = container.exec_run(cmd, demux=False)
        text = (
            out.decode("utf-8", errors="replace")
            if isinstance(out, (bytes, bytearray))
            else str(out or "")
        )
        return int(exit_code), text

    def test(self, inst: Instance, user: str | None, password: str | None) -> dict[str, Any]:
        """Console diagnostics for an instance: winbind trust, and — with a user —
        a real domain-login test plus a per-SSID group-gate preview.

        Mirrors the mschap module the server runs per WLAN request (``ntlm_auth
        --request-nt-key --allow-mschapv2 --require-membership-of``): the base
        wifi gate reproduces the server exactly. The per-SSID rows check the
        account's membership in each SSID's ``allowed_group`` — directly-assigned
        role groups (role-teacher/role-student, <school>-teachers) match the
        server's rlm_ldap check; nested aggregate groups (all-*) would differ
        (token is transitive, rlm_ldap's memberOf is not — see ADR-007). The
        password is passed as one argv element to a single ntlm_auth run and is
        never logged or persisted.
        """
        container = self._get(inst.name)
        result: dict[str, Any] = {"instance": inst.name, "container_running": False}
        if container is None:
            result["detail"] = "container does not exist — run 'lmnradius reconcile' first"
            return result
        container.reload()
        result["container_running"] = bool((container.attrs.get("State") or {}).get("Running"))
        if not result["container_running"]:
            result["detail"] = (
                "container is not running — check 'lmnradius status' / 'lmnradius logs'"
            )
            return result

        # 1) Trust (always) — the precondition for every login.
        result["trust"] = diagnostics.interpret_trust(*self._exec(container, ["wbinfo", "-t"]))

        if user is None:
            return result

        # 2) Domain-login core: password + base wifi gate, exactly as mschap runs it.
        wg = inst.workgroup
        base = [
            "ntlm_auth",
            "--request-nt-key",
            "--allow-mschapv2",
            f"--domain={wg}",
            f"--username={user}",
            f"--password={password}",
        ]
        result["login"] = diagnostics.interpret_ntlm(
            *self._exec(container, [*base, f"--require-membership-of={wg}\\{inst.wifi_group}"])
        )

        # 3) Per-SSID group-gate preview (only worth running once the password is valid).
        gates: list[dict[str, Any]] = []
        if result["login"]["ok"] or result["login"]["code"] == "NT_STATUS_LOGON_FAILURE":
            for ssid in inst.ssids:
                verdict = diagnostics.interpret_ntlm(
                    *self._exec(
                        container, [*base, f"--require-membership-of={wg}\\{ssid.allowed_group}"]
                    )
                )
                gates.append(
                    {
                        "ssid": ssid.name,
                        "group": ssid.allowed_group,
                        "vlan": ssid.vlan,
                        "member": verdict["ok"],
                    }
                )
        result["gates"] = gates
        return result
