# SPDX-FileCopyrightText: Kevin Stenzel
# SPDX-License-Identifier: GPL-3.0-or-later
"""FastAPI application factory for the linuxmuster-radius control plane."""

from __future__ import annotations

import logging
import re
from typing import Any

from docker.errors import DockerException
from fastapi import Depends, FastAPI, HTTPException, Request, status
from fastapi.responses import JSONResponse, PlainTextResponse

from . import ca
from .config import Settings
from .docker_service import DockerService
from .models import (
    DEFAULT_IMAGE,
    CaInitRequest,
    CertIssueRequest,
    Instance,
    InstancePatch,
    UpdateRequest,
)
from .reconciler import Reconciler
from .security import make_verify_token
from .store import Store
from .updater import Updater

audit = logging.getLogger("lmnradius.audit")

# {name} path param flows into Store filenames + docker container/volume names;
# require the same safe instance-name shape the Instance model enforces before we
# ever touch disk or the Docker Engine (fail closed at the API boundary).
_NAME_PARAM_RE = re.compile(r"^[A-Za-z0-9][A-Za-z0-9-]{0,30}$")


def create_app(
    settings: Settings,
    store: Store,
    reconciler: Reconciler,
    docker: DockerService,
    updater: Updater,
) -> FastAPI:
    """Build the FastAPI app wiring routes to the store, reconciler and docker service."""
    verify = make_verify_token(settings)
    app = FastAPI(title="linuxmuster-radius control plane")

    @app.exception_handler(DockerException)
    async def _docker_unreachable(_request: Request, exc: DockerException) -> JSONResponse:
        # Docker daemon down / Engine-API error -> 503 with a clear detail, not a raw 500.
        return JSONResponse(
            status_code=status.HTTP_503_SERVICE_UNAVAILABLE,
            content={"detail": f"docker daemon unreachable: {exc}"},
        )

    # Operator-precondition errors from the apply path (docker_service is fail-closed):
    # a missing EAP cert ("run 'lmnradius cert issue'"), a missing secret file, or a
    # secret the service user cannot read. These carry an actionable message, and
    # swallowing them into a bare 500 hides it from the CLI — exactly what happened on
    # a real first install (create before cert issue). 409: the request is fine, the
    # server-side state is not yet. Messages contain paths, never secret values.
    @app.exception_handler(FileNotFoundError)
    async def _missing_precondition(_request: Request, exc: FileNotFoundError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": str(exc)},
        )

    @app.exception_handler(PermissionError)
    async def _unreadable_precondition(_request: Request, exc: PermissionError) -> JSONResponse:
        return JSONResponse(
            status_code=status.HTTP_409_CONFLICT,
            content={"detail": f"permission denied (check owner lmnradius / mode 0600): {exc}"},
        )

    auth = [Depends(verify)]

    def _require(name: str) -> Instance:
        if not _NAME_PARAM_RE.match(name):
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="invalid instance name",
            )
        inst = store.get(name)
        if inst is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"instance {name!r} not found",
            )
        return inst

    def _check_log_params(tail: int, grep: str | None) -> None:
        if not 1 <= tail <= 10000:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="tail must be between 1 and 10000",
            )
        if grep is not None and len(grep) > 200:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY,
                detail="grep pattern too long (max 200 chars)",
            )

    # ------------------------------------------------------------------ health
    @app.get("/v1/health")
    async def health() -> dict[str, str]:
        return {"status": "ok"}

    @app.get("/v1/version", dependencies=auth)
    async def version() -> dict[str, str]:
        return {"version": settings.version}

    # NOTE: the endpoints below do blocking docker-py / health-poll work; they are
    # plain `def` so FastAPI runs them in a threadpool instead of stalling the event
    # loop (an `update-all` can otherwise hold it for minutes and hang /v1/health).
    @app.post("/v1/reconcile", dependencies=auth)
    def reconcile() -> dict[str, Any]:
        """Re-apply every stored instance (reconverge drift / restore on a fresh host)."""
        audit.info("reconcile all instances")
        return {"reconciled": reconciler.reconcile_all()}

    @app.post("/v1/update-all", dependencies=auth)
    def update_all() -> dict[str, Any]:
        """Lift every instance onto the maintained default image (per-instance rollback)."""
        audit.info("update-all to default image=%s", DEFAULT_IMAGE)
        return {"results": updater.update_all(DEFAULT_IMAGE)}

    # --------------------------------------------------------------- instances
    @app.post(
        "/v1/instances",
        dependencies=auth,
        status_code=status.HTTP_201_CREATED,
    )
    async def create_instance(inst: Instance) -> dict[str, Any]:
        result = reconciler.apply(inst)
        audit.info("create instance name=%s image=%s", inst.name, inst.image)
        return {"instance": inst, "status": result}

    @app.get("/v1/instances", dependencies=auth)
    async def list_instances() -> list[Instance]:
        return store.list()

    @app.get("/v1/instances/{name}", dependencies=auth)
    async def get_instance(name: str) -> Instance:
        return _require(name)

    @app.patch("/v1/instances/{name}", dependencies=auth)
    async def patch_instance(name: str, patch: InstancePatch) -> dict[str, Any]:
        existing = _require(name)
        updates = patch.model_dump(exclude_unset=True)
        # Re-validate the merged instance through Instance's validators. `name` is a
        # real field carried over from the existing record and is absent from
        # InstancePatch, so the identity/name (and thus container/file/mount paths)
        # stays immutable; only the computed `container_name` is dropped before
        # re-validation, since it is derived, not an input.
        merged = Instance.model_validate(
            {**existing.model_dump(exclude={"container_name"}), **updates}
        )
        result = reconciler.apply(merged)
        audit.info(
            "patch instance name=%s fields=%s",
            merged.name,
            sorted(updates.keys()),
        )
        return {"instance": merged, "status": result}

    @app.delete(
        "/v1/instances/{name}",
        dependencies=auth,
        status_code=status.HTTP_204_NO_CONTENT,
    )
    async def delete_instance(name: str) -> None:
        _require(name)
        reconciler.remove(name)
        audit.info("delete instance name=%s", name)

    # ----------------------------------------------------------- lifecycle ops
    @app.post("/v1/instances/{name}/start", dependencies=auth)
    async def start_instance(name: str) -> dict[str, Any]:
        _require(name)
        audit.info("start instance name=%s", name)
        return docker.start(name)

    @app.post("/v1/instances/{name}/stop", dependencies=auth)
    async def stop_instance(name: str) -> dict[str, Any]:
        _require(name)
        audit.info("stop instance name=%s", name)
        return docker.stop(name)

    @app.post("/v1/instances/{name}/restart", dependencies=auth)
    async def restart_instance(name: str) -> dict[str, Any]:
        _require(name)
        audit.info("restart instance name=%s", name)
        return docker.restart(name)

    @app.get("/v1/instances/{name}/status", dependencies=auth)
    async def instance_status(name: str) -> dict[str, Any]:
        _require(name)
        return docker.status(name)

    @app.get("/v1/instances/{name}/logs", dependencies=auth)
    async def instance_logs(
        name: str,
        tail: int = 100,
        since: int | None = None,
        until: int | None = None,
        grep: str | None = None,
    ) -> dict[str, str]:
        """Return the live docker log (radiusd + winbindd on stdout/stderr).

        RADIUS logs go only to stdout/stderr (docker json-file), so unlike squid
        there is no separate gzip-rotated access-log history to query.
        """
        _require(name)
        _check_log_params(tail, grep)
        return {"logs": docker.logs(name, tail=tail, since=since, until=until, grep=grep)}

    # ---------------------------------------------------- digest-pinned updates
    @app.post("/v1/instances/{name}/update", dependencies=auth)
    def update_instance(name: str, body: UpdateRequest) -> dict[str, Any]:
        _require(name)
        audit.info("update request name=%s image=%s", name, body.image)
        return updater.update(name, body.image)

    @app.post("/v1/instances/{name}/rollback", dependencies=auth)
    def rollback_instance(name: str) -> dict[str, Any]:
        _require(name)
        audit.info("rollback request name=%s", name)
        try:
            return updater.rollback(name)
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc

    # ---------------------------------------------------- dedicated EAP CA + certs
    # RSA keygen + signing is CPU-bound blocking work, so these routes are plain
    # `def` (threadpooled). ca.py wraps settings.certs_dir; the passphrase in the
    # body is passed straight through and NEVER logged.
    @app.post("/v1/ca", dependencies=auth, status_code=status.HTTP_201_CREATED)
    def init_ca(body: CaInitRequest) -> dict[str, Any]:
        """Initialise the dedicated EAP CA (the self-signed trust anchor clients pin)."""
        try:
            result = ca.init_ca(
                settings.certs_dir,
                body.passphrase,
                common_name=body.common_name,
                validity_days=body.validity_days,
            )
        except FileExistsError as exc:
            raise HTTPException(status_code=status.HTTP_409_CONFLICT, detail=str(exc)) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        audit.info("init EAP CA common_name=%s", body.common_name)
        return result

    @app.get("/v1/ca", dependencies=auth)
    def get_ca() -> dict[str, Any]:
        """Return the EAP CA status, or 404 if it is not initialised."""
        result = ca.ca_status(settings.certs_dir)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND, detail="EAP CA not initialised"
            )
        return result

    @app.get("/v1/ca/export", dependencies=auth)
    def export_ca() -> PlainTextResponse:
        """Export the CA certificate PEM (the trust anchor to deploy to clients)."""
        try:
            return PlainTextResponse(ca.export_ca(settings.certs_dir))
        except FileNotFoundError as exc:
            raise HTTPException(status_code=status.HTTP_404_NOT_FOUND, detail=str(exc)) from exc

    @app.post(
        "/v1/instances/{name}/cert",
        dependencies=auth,
        status_code=status.HTTP_201_CREATED,
    )
    def issue_cert(name: str, body: CertIssueRequest) -> dict[str, Any]:
        """Sign the EAP server cert for ``name`` (fqdn defaults to its server_fqdn)."""
        inst = _require(name)
        fqdn = body.fqdn or inst.server_fqdn
        try:
            result = ca.issue_server_cert(
                settings.certs_dir,
                name,
                fqdn,
                body.passphrase,
                validity_days=body.validity_days,
            )
        except FileNotFoundError as exc:
            raise HTTPException(
                status_code=status.HTTP_409_CONFLICT,
                detail="initialise the EAP CA first",
            ) from exc
        except ValueError as exc:
            raise HTTPException(
                status_code=status.HTTP_422_UNPROCESSABLE_ENTITY, detail=str(exc)
            ) from exc
        audit.info("issue cert instance=%s fqdn=%s", name, fqdn)
        return result

    @app.get("/v1/instances/{name}/cert", dependencies=auth)
    def get_cert(name: str) -> dict[str, Any]:
        """Return the EAP server-cert status for ``name``, or 404 if unissued."""
        _require(name)
        result = ca.cert_status(settings.certs_dir, name)
        if result is None:
            raise HTTPException(
                status_code=status.HTTP_404_NOT_FOUND,
                detail=f"no EAP server cert for instance {name!r}",
            )
        return result

    return app
