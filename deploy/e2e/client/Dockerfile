# SPDX-FileCopyrightText: Kevin Stenzel
#
# SPDX-License-Identifier: GPL-3.0-or-later

# E2E supplicant image: eapol_test (from wpa_supplicant) drives PEAP-MSCHAPv2
# against the RADIUS image under test and runs the 5-case matrix (assert.sh).
#
# WHY BUILD FROM SOURCE: Debian/Ubuntu's `wpasupplicant` package does NOT ship
# eapol_test (it is a build-time test tool, gated behind CONFIG_EAPOL_TEST=y).
# So we compile just that binary from the pinned wpa_supplicant release, then
# copy it into a slim runtime stage. If the build ever breaks, bump WPA_VERSION.

# ---- build stage: compile eapol_test ----------------------------------------
FROM ubuntu:24.04 AS build
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      build-essential pkg-config \
      libssl-dev libnl-3-dev libnl-genl-3-dev \
      ca-certificates wget \
 && rm -rf /var/lib/apt/lists/*
ARG WPA_VERSION=2.11
WORKDIR /src
RUN wget -qO wpa.tar.gz "https://w1.fi/releases/wpa_supplicant-${WPA_VERSION}.tar.gz" \
 && tar xzf wpa.tar.gz
WORKDIR /src/wpa_supplicant-${WPA_VERSION}/wpa_supplicant
# defconfig already enables PEAP + MSCHAPv2 + TLS(openssl); we only add eapol_test.
RUN cp defconfig .config \
 && printf 'CONFIG_EAPOL_TEST=y\n' >> .config \
 && make eapol_test \
 && install -m 0755 eapol_test /out-eapol_test

# ---- runtime stage ----------------------------------------------------------
FROM ubuntu:24.04
ENV DEBIAN_FRONTEND=noninteractive
RUN apt-get update \
 && apt-get install -y --no-install-recommends \
      libssl3 libnl-3-200 libnl-genl-3-200 ca-certificates \
 && rm -rf /var/lib/apt/lists/*
COPY --from=build /out-eapol_test /usr/local/bin/eapol_test
COPY peap-teacher1.conf peap-student1.conf peap-nowifi1.conf /etc/eap/
COPY assert.sh /usr/local/bin/assert.sh
RUN chmod 0755 /usr/local/bin/assert.sh
ENTRYPOINT ["/usr/local/bin/assert.sh"]
