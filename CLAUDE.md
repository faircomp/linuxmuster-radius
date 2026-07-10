<!--
SPDX-FileCopyrightText: Kevin Stenzel

SPDX-License-Identifier: GPL-3.0-or-later
-->

# CLAUDE.md

## Project overview

**linuxmuster-radius** is a containerized
**WPA2/WPA3-Enterprise RADIUS server for linuxmuster.net 7.x** (Ubuntu 24.04 /
Samba AD): a **FreeRADIUS 3.2** server with **PEAP-MSCHAPv2 via winbind** and
**per-role VLANs** (teachers/students) for school WLANs, **exactly one
self-contained instance per linuxmuster server** (multiple SSIDs are handled
*inside* that instance as config, not as separate containers), managed via a
**REST API + CLI** and running on a **separate RADIUS VM**. The architecture
lives in [`docs/architecture.md`](docs/architecture.md), the security
assumptions in [`docs/threat-model.md`](docs/threat-model.md), the decisions in
[`docs/decisions.md`](docs/decisions.md), the test strategy in
[`docs/test-strategy.md`](docs/test-strategy.md).

| Component | Path | Stack |
|---|---|---|
| Data-plane image (FreeRADIUS) | `image/` | Ubuntu 24.04 · **`freeradius` 3.2** · winbind/samba · `ntlm_auth` · envsubst entrypoint |
| Control plane (REST API) | `controlplane/` | Python · FastAPI · uvicorn · **docker-py** (container lifecycle) · Reconciler + Updater |
| CLI | `controlplane/lmnradius/cli.py` | Python · Typer · httpx (thin client of the REST API, **no direct Docker**) |
| EAP-CA / Cert manager | `controlplane/lmnradius/ca.py` | Private single-purpose EAP-CA (`ca init` / `cert issue` / `ca export`) |
| E2E / Deploy | `deploy/` | docker-compose (Samba AD DC + joined FreeRADIUS + `eapol_test`), instance YAML, client GPO/MDM templates |
| Packaging | `packaging/debian/` | `.deb` via dh-virtualenv, hardened systemd service (lmn73 layout) |
| Tests | `scripts/tests/` | `run.sh` aggregator; heavy tier on **crabbox** (`eapol_test` E2E) |

> **The stack is deliberately Python** (linuxmuster-api7/webui7 are likewise FastAPI/
> Python) — set as the default, overridable (see the ADR in `docs/decisions.md`).

**Security pitfalls (from the threat model — do not violate):**

- **Exactly ONE self-contained instance per server; SSIDs are config, not containers.**
  Multiple SSIDs branch *inside* the one FreeRADIUS instance (virtual-server /
  Called-Station-SSID). A second container only for hard isolation (e.g. a separate
  guest RADIUS). One container per SSID would multiply domain joins, winbind daemons,
  machine accounts and AP shared-secret duplication.
- **PEAP-MSCHAPv2 needs a domain-member join + winbind — the container is stateful.**
  The container joins the Samba AD as a **member server**; `mschap` → `ntlm_auth`
  (`--request-nt-key --allow-mschapv2`) has AD validate the NT-hash via **winbindd**.
  This means a machine-account secret in a `/var/lib/samba` volume (re-join on loss)
  and a second daemon (mini-supervisor) — a deliberate departure from squid's
  stateless keytab model.
- **The EAP-CA + client server-cert pinning is LOAD-BEARING, not optional.** Because
  PEAP's inner MSCHAPv2 is cryptographically weak, security rests on **enforced
  server-certificate validation** against a **dedicated single-purpose EAP-CA**.
  Never reuse the linuxmuster CA (`/etc/linuxmuster/ssl`) or a public/Let's Encrypt
  cert — without server-name pinning any valid cert impersonates the RADIUS server.
- **Users / `wifi` group / role groups / bind user are pure Sophomorix — never
  hand-created.** RADIUS only *consumes* them. The LDAP bind account is the existing
  `global-binduser` (`cn=global-binduser,ou=Management,ou=GLOBAL,dc=…`). Only the
  RADIUS server itself is registered — as a **device with role `server`** in
  `devices.csv` + `linuxmuster-import-devices` on the DC.
- **The UniFi APs are the NAS — `clients.conf` = the AP-management SUBNET(s) as CIDR.**
  Access-Requests come from each AP's *own* IP (the controller is **not** a proxy).
  Support **multiple** subnets via a repeatable `--client-subnet`. Defining only the
  controller IP causes `unknown client`.
- **Parameterize everything, never hardcode:** realm, base DN, group names, client
  subnets, SSID names, VLAN IDs, ports, image digest. Default-school groups are
  unprefixed (`teachers`/`students`/`wifi`), all others `<school>-…`.
- **Docker socket access = root-equivalent.** API bound to `127.0.0.1` + bearer token;
  docker-py only via the one audited service path; behind
  `tecnativa/docker-socket-proxy` on `127.0.0.1`. No `0.0.0.0` bind.

---

## 1. Way of working & mindset

Behave like a senior software engineer with 15+ years of experience in
Python, Linux network services, FreeRADIUS/802.1X/EAP, Active Directory/
Samba/winbind, Docker, and the secure operation of school/network infrastructure.

### Before the code

- **Think first, then code.** For non-trivial changes, present a plan,
  make assumptions explicit, name trade-offs, wait for confirmation.
  Typos/style fixes do not need this.
- **Raise ambiguity, do not decide silently.** Several plausible
  interpretations → name them all, do not secretly pick one.
- **Root cause before symptom.** No workarounds that merely shift the problem.
- **YAGNI rigorously.** No prophylactic abstractions. Build the thin, maintainable
  version. Test: would a senior call it "overengineered"? Then simplify.
- **Validation only at boundaries.** Validate uploads/requests, tokens, LDAP responses,
  env/config; internal function calls not.

### During implementation (surgical changes)

- **Only touch what is necessary.** Do not "improve" adjacent code/comments/formatting.
  Do not refactor what is not broken. Match the existing style.
- **Clean up orphans that YOUR change creates** (unused imports/variables).
  Remove pre-existing dead code only when told to — otherwise mention it, do not delete it.
- **Every changed line must be traceable to the task.**

### For external APIs, formats, and docs — VERIFY

- **Verify instead of fabricate.** Whatever is not 100% backed by the official docs,
  mark explicitly as "not verified". Concretely: before you
  implement any FreeRADIUS/winbind/LDAP/EAP/VLAN behavior, pull the current docs with
  `WebFetch` — above all the **FreeRADIUS wiki/docs** (`rlm_eap`/`eap`, `rlm_mschap`,
  `rlm_ldap`, `rewrite_called_station_id` policy, virtual-servers/`sites-enabled`),
  the **Samba wiki** (`winbindd`, `ntlm_auth --request-nt-key --allow-mschapv2`,
  `net ads join`, member-server setup), **docs.linuxmuster.net** (Sophomorix
  roles/groups, `devices.csv` role `server` + `linuxmuster-import-devices`,
  `/etc/linuxmuster/ssl`, `global-binduser`), **Microsoft/eduroam** (EAP server-cert
  requirements: EKU `serverAuth`, GPO WLAN profile / `.mobileconfig`), and
  **UniFi/Ubiquiti + community.ui.com** (RADIUS profile, `Called-Station-Id`,
  RADIUS-assigned VLAN) — even if the same info appears to be here or in the code.
  Already-checked facts, including their sources, are in
  [`docs/references.md`](docs/references.md).
- **Third-party sources (blogs/forums) are a hint, not a substitute** for the official docs.

### Goals, tests & definition of done

- **Translate every task into a verifiable goal** ("add validation"
  → "test for invalid inputs, then green"; "fix bug" → "reproducing test,
  then green").
- **New function/new flow ⇒ a test for it (mandatory).** Pure logic → unit test
  (`pytest`); a new/changed **auth/SSID-gating/VLAN/lifecycle journey** → E2E on
  crabbox (real `eapol_test` PEAP-MSCHAPv2 through the joined container). No
  "I'll test it later".
- **Negative tests are mandatory.** The catalog in `docs/test-strategy.md` grows per
  roadmap phase (student on teacher-SSID → Reject, wrong password → Reject,
  non-`wifi` user → Reject, `unknown client` from an off-subnet NAS, wrong AP secret,
  SSID/group misconfig, secret perms, auth-off, API 401/403).
- **For multi-step tasks, show a short plan _step → verification_.**
- **Before "done", run all checks for real** (only what exists):
  - **Python (`controlplane/`):** `ruff check`, `ruff format --check`,
    `mypy`, `pytest`. Aggregate target: `bash scripts/tests/run.sh quick`.
  - **Shell (`image/*.sh`, `scripts/`):** `shellcheck`.
  - **FreeRADIUS config:** `radiusd -XC` (config-check, in the container) — green
    before shipping any `sites-enabled`/`mods-enabled`/`clients.conf` change.
  - **Heavy tier / E2E:** on **crabbox** (Docker required) — see below.
- **Run relevant tests routinely, do not claim them.** The Docker/
  winbind/EAP E2E runs on **crabbox** (the dev box has no Docker): keep the box warm,
  after changes to image/auth/SSID-gating/VLAN/lifecycle run
  `LMNRADIUS_ALLOW_REAL=1 bash scripts/tests/run.sh e2e` there and report the
  summary.
- **Monitor CI after triggering it** (`gh run watch`), report the result,
  re-run transient failures deliberately. Not "done" while CI is running/red.

### Communication

- **Direct and short.** Honest about limits ("not verified", "assumption").
- **Push back when necessary** (scope creep, undermining an ADR).
- **Interpret user voice input charitably** (dictation recognition errors →
  respond to the intent).
- **Recommendations with rationale** ("recommendation X, because Y; trade-off Z").
- **Language: German**, technical terms and code identifiers in the original.

### Code conventions

- **Conventional Commits** (`feat:`/`fix:`/`chore:`/`refactor:`/`docs:`/`test:`/
  `perf:`), one commit per logical step; messages in English; tags `vX.Y.Z`.
- **Python:** `ruff` (lint+format) + `mypy` clean; type annotations at public
  boundaries; `pydantic` v2 for models/config; no silent `except:`.
- **Security in the code:** **never log** secrets/tokens/machine-account or
  AP shared secrets; token comparisons constant-time (`hmac.compare_digest`);
  `HTTPBearer(auto_error=False)`; Docker only via the one audited service path
  (docker-py), never directly from the CLI. Strict `pydantic` validation of every
  externally-supplied string (realm, DN, group, SSID, subnet, instance name — they
  flow into filenames/container names/mounts/rendered config).
- **SPDX header & license:** `GPL-3.0-or-later`, © Kevin Stenzel. Every new
  source file gets the REUSE header (`# ` for Python/TOML/YAML/Shell, `<!-- -->` for
  Markdown) — e.g. `reuse annotate --copyright "Kevin Stenzel" --license GPL-3.0-or-later <file>`.
- **Comments only when the why is not obvious** (hidden
  constraints, winbind/DNS/EAP pitfalls, workarounds). The WHAT is in the code.

### Docs maintenance

A code change without a matching docs update counts as incomplete. Before "done", check:
`docs/architecture.md`, `docs/threat-model.md`, `docs/test-strategy.md`,
`docs/decisions.md` (ADRs), `README.md`, `CHANGELOG.md` (from the first version onward). The docs update belongs
in **the same commit** as the code change. Wrong docs are a bug —
fix them, even if not directly part of the change.

## 2. Testing on crabbox (heavy tier)

The dev box only edits + orchestrates — it has **no Docker**. The fast
tier (ruff/mypy/pytest/shellcheck/reuse) runs locally/in CI. The **heavy tier** — the
real docker-compose winbind/EAP E2E (**Samba AD DC + joined FreeRADIUS +
`eapol_test` supplicant**) that proves *teacher→Access-Accept (+correct VLAN) /
student-on-teacher-SSID→Access-Reject / wrong-password→Reject / non-`wifi` user→Reject*,
as well as multischool, update/rollback, and `.deb` install tests — needs real Linux
with **Docker**. **crabbox** leases an ephemeral Proxmox VM for this (provider in
`.claude/settings.json`, token only in the gitignored `.claude/settings.local.json`;
`crabbox doctor`). Rules/details: the `/test` skill (`.claude/skills/test/SKILL.md`).

- **One aggregate runner:** `bash scripts/tests/run.sh [lint|unit|quick|e2e|all]`
  (created in P0/P1). `quick` (default) = lint + unit; `e2e`/`all` run the
  Docker suites and **refuse without `LMNRADIUS_ALLOW_REAL=1`**. Summary:
  `N passed, M failed, K skipped` (exit ≠ 0 on failure); steps dep-gated.
- **Box lifecycle:** `crabbox warmup` → `crabbox run --id <slug> -- 'bash scripts/tests/crabbox_bootstrap.sh'`
  → `crabbox run --id <slug> -- 'LMNRADIUS_ALLOW_REAL=1 bash scripts/tests/run.sh e2e'`
  → `crabbox stop --id <slug>`.
- **Never report "green" without the suite having really passed** — the
  `run.sh` summary is what counts; SKIP means "not verified", not "ok". The
  separate-VM member join is only thinly documented officially: it is **proven in
  this E2E**, not assumed.
- `crabbox warmup`/`run`/`status`/`list`/`connect`/`ssh`/`doctor`/`stop`/`cleanup`
  are pre-approved; `prewarm`/`job` provision/cost → ask first.
- Always `crabbox stop <slug>` (or the 30-min idle timeout), so that no VMs linger.
