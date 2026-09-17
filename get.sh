#!/usr/bin/env bash
# One-line installer for the Balcony Projector, run on the Pi:
#
#   curl -fsSL https://raw.githubusercontent.com/OWNER/balcony-projector/main/get.sh | sudo bash
#
# It clones (or updates) the project into the invoking user's home folder and
# runs install.sh with quiet boot, the projector cable, and the Dallas time zone.
# Set REPO, BRANCH, TIMEZONE or INSTALL_ARGS in the environment to change that:
#
#   curl -fsSL .../get.sh | sudo INSTALL_ARGS="--quiet-boot" bash

set -euo pipefail

REPO="${REPO:-https://github.com/OWNER/balcony-projector.git}"
BRANCH="${BRANCH:-main}"
TIMEZONE="${TIMEZONE:-America/Chicago}"
INSTALL_ARGS="${INSTALL_ARGS:---quiet-boot --projector-link --timezone $TIMEZONE}"

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo:  curl -fsSL .../get.sh | sudo bash"
  exit 1
fi

RUN_USER="${SUDO_USER:-pi}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
TARGET="$RUN_HOME/balcony-projector"

echo "==> Making sure git is installed"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq git

if [[ -d "$TARGET/.git" ]]; then
  echo "==> Updating $TARGET"
  sudo -u "$RUN_USER" git -C "$TARGET" pull --ff-only
else
  echo "==> Cloning into $TARGET"
  sudo -u "$RUN_USER" git clone --branch "$BRANCH" "$REPO" "$TARGET"
fi

cd "$TARGET"
# shellcheck disable=SC2086
exec ./install.sh $INSTALL_ARGS
