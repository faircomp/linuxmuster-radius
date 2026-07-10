---
# SPDX-FileCopyrightText: Kevin Stenzel
#
# SPDX-License-Identifier: GPL-3.0-or-later

name: test
description: Run linuxmuster-radius's real heavy-tier tests on a leased crabbox Linux box — the docker-compose winbind/EAP E2E (Samba AD DC + a joined FreeRADIUS member + eapol_test) the dev box can't run. Use when asked to run integration/e2e/heavy tests, verify the PEAP-MSCHAPv2 auth / per-SSID group-gate / VLAN flow on real Linux, or before a release.
---

# Testing linuxmuster-radius on crabbox

The fast tier (ruff / mypy / pytest / shellcheck / reuse) runs anywhere and in CI.
The **heavy tier** — the real docker-compose stack (**Samba AD DC + the FreeRADIUS
image joined as a domain MEMBER + an `eapol_test` supplicant**) that proves
*teacher→Accept+VLAN 20 / student-on-teacher-SSID→Reject / student→Accept+VLAN 10 /
wrong-password→Reject / non-`wifi`→Reject* — needs a real Linux box with **Docker**,
which the sandboxed dev box lacks. This suite is **authored here, run by the
operator**: crabbox leases an ephemeral Proxmox VM, rsyncs the working tree, runs
the suite, and tears down.

Provider env (proxmox) comes from `.claude/settings.json` (the **non-secret** provider
config). The `CRABBOX_PROXMOX_TOKEN_SECRET` lives **only** in the gitignored
`.claude/settings.local.json` — never commit it. Confirm with `crabbox doctor`.

## Single-box flow (warm once → reuse the slug → stop)

1. **Warm** a box and note its slug: `crabbox warmup` → e.g. `slug=silver-lobster`.
2. **Hydrate** it once (installs Docker + openssl, pre-pulls `ubuntu:24.04`, builds the
   FreeRADIUS image from `image/`; the Samba-AD-DC and the `eapol_test` client are BUILT
   from `deploy/e2e/dc` and `deploy/e2e/client` by compose, not pulled):
   `crabbox run --id <slug> -- 'bash scripts/tests/crabbox_bootstrap.sh'`
3. **Run** the aggregator — one command, dependency-gated:
   - `crabbox run --id <slug> -- 'bash scripts/tests/run.sh quick'`                        (ruff + mypy + pytest + shellcheck + reuse)
   - `crabbox run --id <slug> -- 'LMNRADIUS_ALLOW_REAL=1 bash scripts/tests/run.sh e2e'`   (docker-compose winbind/EAP E2E)
   - `crabbox run --id <slug> -- 'LMNRADIUS_ALLOW_REAL=1 bash scripts/tests/run.sh all'`   (quick + e2e)
4. **Inspect** on failure: `crabbox ssh --id <slug>` (live), or read the newest
   `.crabbox/captures/*.tar.gz` (logs, timings, ready-made stop command).
5. **Stop** when done: `crabbox stop --id <slug>`.

`run.sh` prints `N passed, M failed, K skipped` and exits non-zero on any failure.
`e2e`/`all` refuse to run without `LMNRADIUS_ALLOW_REAL=1` (a guard so heavy suites
never fire by accident on the dev box).

> The `scripts/tests/run.sh` aggregator and `crabbox_bootstrap.sh` are the
> established entry points; `e2e` shells out to `scripts/tests/e2e_radius.sh`.
> Wire new heavy tests into that aggregator rather than one-off scripts.

## What the heavy E2E must actually prove (the matrix)

The compose stack has three services on a user-defined bridge with static IPs:
`dc` (the Samba AD DC — KDC + AD LDAP + internal DNS), `radius` (the image under test —
its **container hostname == `SERVER_FQDN`**, DNS → `dc`), and `client` (`eapol_test`).
Fixtures live on a **PLAIN Samba AD DC**, provisioned directly by
`deploy/e2e/dc/bootstrap.sh` with `samba-tool` (NOT via Sophomorix): users `teacher1` /
`student1` / `nowifi1`, group `wifi` (with `teacher1` + `student1`), role groups
`teachers` (with `teacher1`) and `students` (with `student1`), a join account
(`radiusjoin`), and the `binduser` LDAP bind account. A DNS **A-record for
`SERVER_FQDN`** points at the `radius` container IP.

Assertions (driven from the `client` service via `eapol_test`, PEAP-MSCHAPv2):

| # | identity | SSID (`Called-Station-Id`) | expect |
|---|---|---|---|
| 1 | `teacher1` | teacher SSID | **Access-Accept** + `Tunnel-Private-Group-Id = 20` |
| 2 | `student1` | teacher SSID | **Access-Reject** (right SSID, wrong role) |
| 3 | `student1` | student SSID | **Access-Accept** + `Tunnel-Private-Group-Id = 10` |
| 4 | `teacher1` (**WRONG** password) | teacher SSID | **Access-Reject** |
| 5 | `nowifi1` (not in `wifi`) | either SSID | **Access-Reject** (base gate) |

Passing all five proves, end-to-end: **member domain-join inside the container**
(`net ads join MEMBER`), **`winbindd` + `winbindd_privileged` perms** (the `freerad`
service user reading the privileged pipe), **`ntlm_auth` NT-hash validation** against
the DC, **`rewrite_called_station_id` → `Called-Station-SSID` branching**, the
**`rlm_ldap` recursive group check**, and the **`Tunnel-*` RADIUS-assigned VLAN** reply
landing on the outer Access-Accept. Case 4 proves auth is really enforced (not an
allow-all fluke); cases 2 vs 5 split *authenticated-but-role-denied* from
*failed-the-base-`wifi`-gate*. That split is what makes this a genuine **PROOF**, not a
smoke test — assert on the Accept/Reject result **and** the VLAN attribute, not just on
the process exit alone.

> **HONEST LIMIT — state it in every report.** The E2E uses a **PLAIN Samba AD DC**:
> users, groups and the domain-join account are created directly with `samba-tool`. It
> therefore does **NOT** exercise the linuxmuster **Sophomorix / `devices.csv`
> role=`server` + `linuxmuster-import-devices`** member-registration path (ADR-006).
> Whether that path pre-creates the machine account so a later `net ads join MEMBER`
> **adopts** it cleanly — or whether a **delegated join account** with rights to
> create/reset the machine account is required — is still **NICHT VERIFIZIERT** and
> needs a real linuxmuster server (documented in `docs/radius-and-ad.md` and
> `scripts/provision-radius-account.sh`). **Green here means "the auth / SSID-gate /
> VLAN data plane works"; it does NOT mean "the lmn member-registration path works."**

## The image env contract (the compose must set exactly these)

From `image/entrypoint.sh` — the container aborts **fail-closed** if any of these is
missing, so the `radius` service must set the contract verbatim:

- **Required env:** `INSTANCE`, `REALM` (UPPERCASE), `WORKGROUP` (NetBIOS short name),
  `SERVER_FQDN` (== the `radius` container hostname, forward-resolvable via the DC's
  DNS), `LDAP_SERVER` (`ldaps://<dc>`), `LDAP_BASE_DN`, `LDAP_BIND_DN`, `WIFI_GROUP`.
- **Secret FILE mounts** (the env var holds the *path*, never the value):
  `LDAP_BIND_SECRET` (the LDAP bind-user password, one line), `JOIN_SECRET` (a samba
  `-A` authfile: `username = …` / `password = …` / `domain = <WORKGROUP>`), `EAP_CA`,
  `EAP_CERT`, `EAP_KEY`.
- **Per-instance config**, read-only at `/etc/lmnradius/instance.d/{clients.conf,ssid-policy}`.
  In the E2E, mount these **directly** — the control-plane rendering is already
  unit-tested, so the E2E must not re-test it. `clients.conf` = one `client{}` for the
  `client` subnet carrying the shared secret; `ssid-policy` = the rendered per-SSID
  gate with the `Tunnel-*` VLAN branch (teacher SSID → `teachers` → 20, student SSID →
  `students` → 10, else `reject`).
- **A volume at `/var/lib/samba`** (the machine-account secret from the join). Use a
  **fresh** volume each run so the join is reproducible; a populated volume means
  "already joined" and is never re-joined.
- **`dns: [<DC ip>]`** on the `radius` service so the SRV `_ldap._tcp` record **and**
  the `SERVER_FQDN` A-record resolve — Docker's embedded DNS is not enough.

Generate the **EAP certs with `openssl`**, mirroring what `lmnradius cert issue` emits:
a dedicated CA, then a server cert with EKU **`serverAuth` `1.3.6.1.5.5.7.3.1`** +
**`id-kp-eapOverLAN` `1.3.6.1.5.5.7.3.14`** and **`SAN=DNS:SERVER_FQDN`**. Do **not**
reuse a public/linuxmuster CA — the pin is the entire point (see `docs/certs-and-ca.md`).

## Driving PEAP-MSCHAPv2 with `eapol_test`

`eapol_test` ships with `wpa_supplicant`/`hostapd` (in the client image). Per case:

- **Set the SSID via the `Called-Station-Id` attribute:**
  `eapol_test -c <conf> -a <radius-ip> -s <shared-secret> -N30:s:<AP-MAC>:<SSID>`
  (attr **30** = `Called-Station-Id`, RFC 3580 format `<AP-MAC>:<SSID>`).
  `rewrite_called_station_id` then populates `Called-Station-SSID` for the
  `ssid-policy` branch.
- The `eapol_test` conf sets `eap=PEAP`, `phase2="auth=MSCHAPV2"`, and the per-case
  `identity` / `password`.
- **Pin the server cert in at least the happy path:** `ca_cert=<EAP CA PEM>` +
  `domain_suffix_match=<SERVER_FQDN>`. This exercises the client-side certificate
  validation the whole security model rests on.
- **Access-Accept ⇒ `eapol_test` exits 0; a reject/failure ⇒ non-zero.** Assert the
  exit code, **then** assert the VLAN: grep `eapol_test`'s printed Access-Accept
  attributes for `Tunnel-Private-Group-Id` == the expected value (20 / 10).

## Pitfalls (keep them true or the E2E lies)

- **Privileged DC:** a real Samba AD DC needs `privileged: true` (or elevated caps) and
  ports 88/389/53 — crabbox's full VM handles it; a locked-down CI runner may not.
- **The winbind privileged pipe.** `freerad` must be in group `winbindd_priv` (a
  **build-time** `usermod` in the image) to read
  `/var/lib/samba/winbindd_privileged` (`root:winbindd_priv 0750`); without it
  PEAP-MSCHAPv2 fails **silently**. The entrypoint only re-asserts the dir perms.
- **DC-side `ntlm auth`.** The DC's `smb.conf` needs
  `ntlm auth = mschapv2-and-ntlmv2-only`; the modern Samba default `ntlmv2-only`
  **forbids** MSCHAPv2, so *every* login fails even with a correctly-joined member.
  Set it on the E2E DC.
- **DNS / hostname canonicalization is the flakiest part.** The `radius` container
  hostname MUST equal `SERVER_FQDN` and forward-resolve via the DC; the image bakes
  `krb5.conf` with `rdns`/`dns_canonicalize_hostname` off + `SASL_NOCANON on`. Point the
  `radius` service at the DC as resolver (`dns: [<ip>]`), not Docker embedded DNS.
- **NTP skew < 5 min** or the join fails with `KRB_AP_ERR_SKEW`.
- **Password complexity:** set the DC to complexity off
  (`samba-tool domain passwordsettings set --complexity=off`, or `NOCOMPLEXITY=true`)
  or scripted user creation fails.
- **Cert EKU:** a server cert **without** `serverAuth` is rejected by strict
  supplicants — mirror `cert issue`'s EKUs + SAN exactly.
- **Fresh state each run:** a fresh `/var/lib/samba` volume so the join is reproducible;
  add an explicit readiness wait (DC provision + KDC/winbind trust ~30–90 s; the
  container's own bounded wait is `WINBIND_WAIT=60`).
- **Provider template:** the Proxmox template needs DHCP + cloud-init + `ciuser=crabbox`.
  `crabbox doctor` green does **not** prove the template boots + SSHes.
- **Lease sequentially** and keep every crabbox call `timeout`-bounded.

## Rules

- **A box that fails sync sanity is not a debugging target.** Stop it, warm a fresh one,
  rerun.
- **Never claim a suite is green unless it actually passed** — report the `run.sh`
  `N passed, M failed, K skipped` line **verbatim**; treat SKIP as "not verified", not
  "ok". Never report a passing E2E without that summary line.
- **Always state the honest limit.** The separate-VM member join is only thinly
  documented officially: this E2E is where the **plain-AD** auth/gate/VLAN path is
  *proven*, not assumed — but the **Sophomorix / `devices.csv`** registration path is
  **NICHT VERIFIZIERT** here (see the matrix's HONEST LIMIT).
- `crabbox warmup`/`run`/`status`/`list`/`connect`/`ssh`/`doctor`/`stop`/`cleanup`
  are pre-approved; `prewarm`/`job` provision/cost → ask first.
- Always `crabbox stop <slug>` (or rely on the 30 m idle timeout) so VMs don't linger.
