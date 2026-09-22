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

import fcntl
import ipaddress
import logging
import os
import socket
import struct
import time
from pathlib import Path
from typing import Any, Optional

import docker
from docker.errors import APIError, ContainerError, ImageNotFound, NotFound
from docker.models.containers import Container
from docker.types import LogConfig

from . import diagnostics
from .models import Instance, ldap_url_parts
from .render import render_clients_conf, render_ssid_policy

logger = logging.getLogger("lmnradius.docker")

# -- container-side mount points consumed by image/entrypoint.sh ---------------
_INSTANCE_D_MOUNT = "/etc/lmnradius/instance.d"  # entrypoint MOUNT_D (clients.conf + ssid-policy)
_SECRETS_MOUNT = "/run/secrets"  # ro-mounted secret FILES (paths land in the env)
_CERTS_MOUNT = "/run/secrets/eap"  # ro-mounted EAP cert material (matches entrypoint EAP_* paths)
_LDAP_CA_MOUNT = "/run/secrets/ldap/ca.pem"  # ro-mounted DC CA bundle (entrypoint LDAP_CA)
_SAMBA_STATE = "/var/lib/samba"  # entrypoint STATEDIR persistent volume

# EAP cert material filenames inside ``certs_dir/<name>/`` (control-plane cert output).
_CA_FILE = "ca.pem"
_CERT_FILE = "server.pem"
_KEY_FILE = "server.key"

# How long ensure_running() watches a freshly started container before answering.
# The entrypoint needs ~10-20 s to join/verify the trust and pass `radiusd -XC`; a
# config problem (e.g. a client on loopback) makes it exit within that window and the
# restart policy starts the crash loop -- which used to be reported as "running".
_SETTLE_TIMEOUT = 45.0
_SETTLE_POLL = 1.0
_SIOCGIFADDR = 0x8915


def detect_host_ip(target_host: str) -> Optional[str]:
    """Best-effort LAN IPv4 of this host: the source address the kernel would use to
    reach ``target_host`` (the DC; no packet is sent), else the address of the
    default-route interface. ``None`` if neither can be determined."""
    try:
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            sock.connect((target_host, 389))
            candidate = sock.getsockname()[0]
        if not ipaddress.ip_address(candidate).is_loopback:
            return str(candidate)
    except (OSError, ValueError):
        pass
    try:
        with open("/proc/net/route", encoding="ascii") as fh:
            rows = [line.split() for line in fh.readlines()[1:]]
        ifname = next(row[0] for row in rows if len(row) > 1 and row[1] == "00000000")
        with socket.socket(socket.AF_INET, socket.SOCK_DGRAM) as sock:
            packed = fcntl.ioctl(
                sock.fileno(), _SIOCGIFADDR, struct.pack("256s", ifname[:15].encode("ascii"))
            )
        return socket.inet_ntoa(packed[20:24])
    except (OSError, ValueError, StopIteration):
        return None


def state_view(attrs: dict[str, Any]) -> dict[str, Any]:
    """Derive the honest runtime view from a container's inspect ``attrs``.

    ``running`` is false while Docker is restarting the container, and
    ``crash_looping`` is set when the restart policy already had to restart it
    (``RestartCount`` is reset whenever ensure_running() recreates the container) and
    it is not healthy -- the case the old five-key status masked as "running".
    """
    state: dict[str, Any] = attrs.get("State", {}) or {}
    health: Optional[str] = None
    health_state = state.get("Health")
    if isinstance(health_state, dict):
        status_value = health_state.get("Status")
        health = status_value if isinstance(status_value, str) else None
    restarting = bool(state.get("Restarting", False))
    running = bool(state.get("Running", False)) and not restarting
    try:
        restart_count = int(attrs.get("RestartCount") or 0)
    except (TypeError, ValueError):
        restart_count = 0
    view: dict[str, Any] = {
        "running": running,
        "health": health,
        "restart_count": restart_count,
        "crash_looping": restarting or (restart_count > 0 and health != "healthy"),
    }
    if not running:
        view["exit_code"] = state.get("ExitCode")
    return view


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
        host_ip: Optional[str] = None,
        client: Optional[docker.DockerClient] = None,
    ) -> None:
        self.docker_host: Optional[str] = docker_host
        self.secrets_dir: str = secrets_dir
        self.certs_dir: str = certs_dir
        self.render_dir: str = render_dir
        self.container_bind_ip: str = container_bind_ip
        self.log_max_size: str = log_max_size
        self.log_max_file: int = log_max_file
        # Explicit LAN address for the A record (Settings.host_ip); None = detect per apply.
        self.host_ip: Optional[str] = host_ip
        self.settle_timeout: float = _SETTLE_TIMEOUT
        if client is not None:
            self.client: docker.DockerClient = client
        else:
            self.client = (
                docker.DockerClient(base_url=docker_host) if docker_host else docker.from_env()
            )

    # -- helpers -----------------------------------------------------------

    @staticmethod
    def _container_name(name: str) -> str:
        return f"lmnradius-{name}"

    @staticmethod
    def _volume_name(name: str) -> str:
        return f"lmnradius-samba-{name}"

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

        Every key here is read by the entrypoint. The ``*_SECRET``, ``EAP_*`` and
        ``LDAP_CA`` values are the *container* paths of the read-only file mounts set
        up in :meth:`_mounts` — they hold PATHS, never secret material. ``HOST_IP`` is
        the LAN address the container registers as the A record of ``server_fqdn``
        (instead of its own bridge address); omitted when it cannot be determined."""
        env = {
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
        if inst.ldap_ca is not None:
            env["LDAP_CA"] = _LDAP_CA_MOUNT
        host_ip = self.host_ip or detect_host_ip(ldap_url_parts(inst.ldap_server)[1])
        if host_ip:
            env["HOST_IP"] = host_ip
        else:
            logger.warning(
                "could not determine this host's LAN address; %s will not register a DNS A "
                "record (set host_ip in config.yml)",
                inst.server_fqdn,
            )
        return env

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
        env = self.env_for(inst)
        volumes = self._mounts(inst, env, require_eap=True)
        volumes[render_host] = {"bind": _INSTANCE_D_MOUNT, "mode": "ro"}

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
            volumes=volumes,
            ports={
                f"{_AUTH_PORT}/udp": (self.container_bind_ip, _AUTH_PORT),
                f"{_ACCT_PORT}/udp": (self.container_bind_ip, _ACCT_PORT),
            },
            **self._hardening(),
        )
        return self._wait_settled(inst.name)

    def _mounts(
        self, inst: Instance, env: dict[str, str], require_eap: bool
    ) -> dict[str, dict[str, str]]:
        """Resolve the read-only file mounts + the state volume for ``inst``.

        Fails closed on a missing secret, EAP cert material (load-bearing for PEAP
        server-cert pinning, docs/threat-model / ADR-005) or a missing pinned DC CA.
        Bind targets come straight from ``env`` so the mount paths and the env paths
        the entrypoint reads can never drift apart. ``require_eap=False`` (the one-off
        leave run) tolerates absent EAP files -- leaving the domain needs none."""
        join_host = self._resolve_under(self.secrets_dir, inst.join_secret)
        ldap_bind_host = self._resolve_under(self.secrets_dir, inst.ldap_bind_secret)
        for label, path in (("join_secret", join_host), ("ldap_bind_secret", ldap_bind_host)):
            if not os.path.isfile(path):
                raise FileNotFoundError(f"{label} file missing: {path} (mount it read-only, 0600)")
        volumes: dict[str, dict[str, str]] = {
            join_host: {"bind": env["JOIN_SECRET"], "mode": "ro"},
            ldap_bind_host: {"bind": env["LDAP_BIND_SECRET"], "mode": "ro"},
            # Persistent AD machine-account secret (secrets.tdb); survives recreate so
            # the one-time domain join is not repeated on every reconcile/update.
            self._volume_name(inst.name): {"bind": _SAMBA_STATE, "mode": "rw"},
        }

        cert_dir = self._resolve_under(self.certs_dir, inst.name)
        for fname, key in ((_CA_FILE, "EAP_CA"), (_CERT_FILE, "EAP_CERT"), (_KEY_FILE, "EAP_KEY")):
            path = os.path.join(cert_dir, fname)
            if os.path.isfile(path):
                volumes[path] = {"bind": env[key], "mode": "ro"}
            elif require_eap:
                raise FileNotFoundError(
                    f"EAP cert material missing: {path} "
                    "(run 'lmnradius cert issue' — server-cert pinning is load-bearing)"
                )

        if inst.ldap_ca is not None:
            ldap_ca_host = self._resolve_under(cert_dir, inst.ldap_ca)
            if not os.path.isfile(ldap_ca_host):
                raise FileNotFoundError(
                    f"ldap_ca file missing: {ldap_ca_host} "
                    f"(run 'lmnradius set-ldap-ca {inst.name} --ldap-ca <file>')"
                )
            volumes[ldap_ca_host] = {"bind": env["LDAP_CA"], "mode": "ro"}
        return volumes

    def _hardening(self) -> dict[str, Any]:
        """The hardened run profile shared by the instance and the one-off leave run."""
        return {
            "read_only": True,
            # Read-only rootfs; the entrypoint writes only to tmpfs (/run, /tmp) and the
            # /var/lib/samba volume (docs/architecture.md §3: rootfs partially relaxed for
            # the Samba machine-account state).
            "tmpfs": {"/run": "", "/tmp": ""},
            "cap_drop": ["ALL"],
            # root copies the secrets + chowns the tmpfs config, then radiusd/winbindd drop
            # to 'freerad'. FOWNER is deliberately NOT granted (copy_secret chmods before chown).
            "cap_add": ["SETUID", "SETGID", "DAC_OVERRIDE", "CHOWN"],
            "security_opt": ["no-new-privileges:true"],
            # Docker json-log is capped (live view only); radiusd/winbindd log to stdout.
            "log_config": LogConfig(
                type="json-file",
                config={"max-size": self.log_max_size, "max-file": str(self.log_max_file)},
            ),
        }

    def _wait_settled(self, name: str) -> dict[str, Any]:
        """Watch a freshly started container until it is healthy, unhealthy or crash-
        looping (bounded by ``settle_timeout``), then return its honest status."""
        deadline = time.monotonic() + self.settle_timeout
        while True:
            st = self.status(name)
            if (
                st["health"] in ("healthy", "unhealthy")
                or st.get("crash_looping")
                or not st["running"]
                or time.monotonic() >= deadline
            ):
                return st
            time.sleep(_SETTLE_POLL)

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

    def remove(self, inst: Instance) -> dict[str, Any]:
        """Tear the instance down completely: stop the container, leave the domain
        (DNS record + computer account, via the image's ``leave`` action), remove the
        container, the machine-account volume and the rendered config.

        The domain leave is reported, not enforced: a DC that is unreachable must not
        make ``rm`` impossible, so the result carries ``{"ok", "detail"}`` and the CLI
        tells the operator what to clean up by hand. ``certs_dir/<name>/`` (EAP server
        cert/key + pinned DC CA) is deliberately kept."""
        name = inst.name
        container = self._get(name)
        if container is not None:
            try:
                container.stop(timeout=15)
            except APIError as exc:
                logger.warning("stop %s before leave failed: %s", name, exc)
        leave = self._leave_domain(inst)
        container = self._get(name)
        if container is not None:
            container.remove(force=True)
        try:
            self.client.volumes.get(self._volume_name(name)).remove(force=True)
        except NotFound:
            pass
        # Drop the rendered per-instance config (it embeds the AP shared secret).
        try:
            inst_dir = self._resolve_under(self.render_dir, name)
        except ValueError:
            return leave
        if os.path.isdir(inst_dir):
            for fname in (_CLIENTS_FILE, _SSID_POLICY_FILE):
                fpath = os.path.join(inst_dir, fname)
                if os.path.isfile(fpath):
                    os.remove(fpath)
            try:
                os.rmdir(inst_dir)
            except OSError:
                pass
        return leave

    def _leave_domain(self, inst: Instance) -> dict[str, Any]:
        """Run the image's one-off ``leave`` action against the instance's state volume.

        Same image, env, secrets and hardening as the instance; the entrypoint removes
        the A record of ``server_fqdn`` and deletes the computer account with the join
        credentials, then exits (non-zero = something is left on the DC)."""
        try:
            self.client.volumes.get(self._volume_name(inst.name))
        except NotFound:
            return {"ok": True, "attempted": False, "detail": "never joined (no state volume)"}
        try:
            env = self.env_for(inst)
            volumes = self._mounts(inst, env, require_eap=False)
        except (FileNotFoundError, ValueError) as exc:
            return {"ok": False, "attempted": False, "detail": f"cannot run the leave: {exc}"}
        try:
            out = self.client.containers.run(
                inst.image,
                command=["leave"],
                hostname=inst.server_fqdn,
                environment=env,
                volumes=volumes,
                detach=False,
                remove=True,
                stdout=True,
                stderr=True,
                healthcheck={"test": ["NONE"]},
                **self._hardening(),
            )
            return {"ok": True, "attempted": True, "detail": _tail(out)}
        except ContainerError as exc:
            return {"ok": False, "attempted": True, "detail": _tail(exc.stderr)}
        except (ImageNotFound, APIError) as exc:
            return {"ok": False, "attempted": True, "detail": str(exc)}

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
        view = state_view(container.attrs)

        image: Optional[str] = None
        image_obj = container.image
        if image_obj is not None and image_obj.tags:
            image = image_obj.tags[0]

        result: dict[str, Any] = {"name": name, "exists": True, **view, "image": image}
        if view["crash_looping"]:
            # Surface the reason so the operator does not have to dig through
            # `docker logs`. A wide window: the entrypoint's FATAL line is followed by
            # the full `radiusd -XC` dump (hundreds of lines) before the restart.
            try:
                lines = container.logs(tail=600).decode("utf-8", errors="replace").splitlines()
            except APIError:
                lines = []
            result["last_error"] = pick_error_line(lines)
        return result

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


def pick_error_line(lines: list[str]) -> str:
    """The most telling line of a crash-looping container's log: the entrypoint's
    last FATAL line, else the last radiusd/stunnel error, else the last line."""
    for marker in ("FATAL", "Error:", "LOG3["):
        hits = [line for line in lines if marker in line]
        if hits:
            return hits[-1].strip()
    return (lines or ["(no log output)"])[-1].strip()


def _tail(data: Any, lines: int = 8) -> str:
    """Last ``lines`` lines of container output (bytes or str) as one string."""
    if isinstance(data, (bytes, bytearray)):
        text = data.decode("utf-8", errors="replace")
    else:
        text = str(data or "")
    return "\n".join(text.strip().splitlines()[-lines:])
