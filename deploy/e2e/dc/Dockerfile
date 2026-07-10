# SPDX-FileCopyrightText: Kevin Stenzel
#
# SPDX-License-Identifier: GPL-3.0-or-later

# E2E-only Samba Active Directory Domain Controller (KDC + LDAP(S) + DNS).
# NOT a production artifact — it stands in for a real linuxmuster server so the
# RADIUS image under test has an AD to member-join and authenticate against.
#
# We build our own DC (rather than a black-box image) because the E2E needs full
# control over the fixtures: the exact groups/users/memberships, a join account,
# an LDAP bind account, the radius A-record, and 'ntlm auth =
# mschapv2-and-ntlmv2-only' on the DC (see docs/radius-and-ad.md §4).
FROM ubuntu:24.04

ENV DEBIAN_FRONTEND=noninteractive

# samba            — the AD DC daemon (/usr/sbin/samba) + its own internal winbindd/DNS
# samba-common-bin — samba-tool (provision, user/group/dns subcommands)
# smbclient        — handy for diagnostics
# krb5-user        — kinit/klist for Kerberos plumbing
# ldb-tools        — ldbsearch for offline directory inspection
# dnsutils         — `host` for the healthcheck DNS probe
# python3          — used by the entrypoint to patch smb.conf robustly
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      samba \
      samba-common-bin \
      smbclient \
      krb5-user \
      ldb-tools \
      dnsutils \
      python3 \
 && rm -rf /var/lib/apt/lists/*

COPY entrypoint.sh /usr/local/bin/entrypoint.sh
COPY bootstrap.sh  /usr/local/bin/bootstrap.sh
RUN chmod 0755 /usr/local/bin/entrypoint.sh /usr/local/bin/bootstrap.sh

# DNS(53), Kerberos(88), LDAP(389)/LDAPS(636), kpasswd(464). Informational.
EXPOSE 53 53/udp 88 88/udp 389 636 464 464/udp

ENTRYPOINT ["/usr/local/bin/entrypoint.sh"]
