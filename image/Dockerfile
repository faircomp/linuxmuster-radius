# SPDX-FileCopyrightText: Kevin Stenzel
#
# SPDX-License-Identifier: GPL-3.0-or-later

# linuxmuster-radius — data-plane image
# One generic, self-contained FreeRADIUS 3.2 image, driven entirely by env +
# mounted secrets/config. Exactly ONE container per linuxmuster server; MULTIPLE
# SSIDs are handled inside this one instance (Called-Station-SSID branching), NOT
# as separate containers.
#
# Auth = PEAP-MSCHAPv2 via winbind: the container joins the Samba AD as a MEMBER
# server, so `mschap` -> `ntlm_auth` has the AD validate the NT-hash (the AD never
# releases the NT-hash over LDAP, so an LDAP bind alone cannot do MSCHAPv2). That
# is why this image is STATEFUL (machine-account secret on a /var/lib/samba volume)
# and ships a second daemon (winbindd), unlike the stateless keytab model of the
# sibling linuxmuster-squid image.
#
# NOTE: on Ubuntu 24.04 the daemon binary is /usr/sbin/freeradius; there is NO
# `radiusd` symlink (verified against the noble filelist). The config tree lives
# under /etc/freeradius/3.0/ (owned by the freeradius-config dependency); loadable
# modules are /usr/lib/freeradius/rlm_*.so.
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# Package roles:
#   freeradius / freeradius-ldap / freeradius-utils
#       core server; rlm_ldap for per-SSID group/VLAN authorization; radclient for
#       the Status-Server healthcheck probe.
#   samba / winbind / libnss-winbind / samba-common-bin
#       AD member join (`net ads join`) + winbindd + ntlm_auth (NT-hash validation)
#       + NSS resolution of AD users/groups.
#   krb5-user
#       kinit/klist and the Kerberos client libs winbind/net rely on.
#   gettext-base
#       envsubst — the entrypoint renders config with an explicit variable allow-list.
#   openssl / ca-certificates
#       TLS plumbing for LDAPS to the DC and EAP cert handling.
#   ldap-utils
#       ldapsearch — bind-user / group-gate diagnostics.
#   tini
#       PID 1 init; forwards signals to and reaps the supervised daemons.
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      freeradius \
      freeradius-ldap \
      freeradius-utils \
      samba \
      winbind \
      libnss-winbind \
      samba-common-bin \
      krb5-user \
      gettext-base \
      openssl \
      ca-certificates \
      ldap-utils \
      tini \
 && rm -rf /var/lib/apt/lists/* \
 && mkdir -p \
      /etc/lmnradius/instance.d \
      /etc/freeradius/3.0/templates \
      /var/log/freeradius \
 && chown -R freerad:freerad /var/log/freeradius

# winbind member-server prerequisite (Samba wiki: "Authenticating FreeRADIUS against
# Active Directory"): ntlm_auth talks to winbindd through the privileged pipe under the
# smb.conf 'state directory' (/var/lib/samba, the persistent volume) as
# <statedir>/winbindd_privileged, owned root:winbindd_priv 0750. The FreeRADIUS service
# user `freerad` must be in the `winbindd_priv` group or every MSCHAPv2 auth fails with a
# winbind permission error. That group membership is the BUILD-TIME step; winbindd
# self-provisions the pipe dir at runtime and the entrypoint re-asserts its perms.
RUN set -eu; \
    getent group winbindd_priv >/dev/null || groupadd -r winbindd_priv; \
    usermod -aG winbindd_priv freerad

# Fail the build early if a binary we rely on is missing (all verified present in the
# noble packages above). Mirrors squid's helper assertion.
RUN set -eu; for b in \
      /usr/sbin/freeradius \
      /usr/bin/ntlm_auth \
      /usr/sbin/winbindd \
      /usr/bin/wbinfo \
      /usr/bin/radclient \
      /usr/bin/envsubst ; do \
      test -x "$b" || { echo "MISSING binary: $b" >&2; exit 1; }; \
    done

COPY templates/       /etc/freeradius/3.0/templates/
COPY entrypoint.sh    /usr/local/bin/entrypoint.sh
COPY healthcheck.sh   /usr/local/bin/healthcheck.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/healthcheck.sh

# RADIUS authentication + accounting (UDP). The UniFi APs are the NAS and send from
# their own IP; clients.conf is mounted per instance under /etc/freeradius/3.0/instance.d/.
EXPOSE 1812/udp 1813/udp

# Healthy only when BOTH the auth backend and the listener are up (see healthcheck.sh):
# `wbinfo -t` proves the AD trust, a radclient Status-Server probe proves radiusd listens.
# Longer start-period than squid: the domain join + winbind warm-up take longer than a
# stateless proxy start.
HEALTHCHECK --interval=30s --timeout=10s --start-period=45s --retries=3 \
  CMD /usr/local/bin/healthcheck.sh

# tini is PID 1; the entrypoint supervises winbindd + radiusd and tears both down on
# exit. Read-only-rootfs friendly: the entrypoint writes only to tmpfs (/run) and the
# /var/lib/samba volume, never to /etc at runtime.
ENTRYPOINT ["/usr/bin/tini", "--", "/usr/local/bin/entrypoint.sh"]
