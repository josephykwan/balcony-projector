#!/usr/bin/env bash
# Removes the Balcony Projector service and undoes what install.sh changed.
# Keeps your videos in ~/media and this folder; delete those yourself if you want.
#
#   sudo ./uninstall.sh

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo:  sudo ./uninstall.sh"
  exit 1
fi

SERVICE=balcony-projector

echo "==> Stopping and removing the $SERVICE service"
systemctl disable --now "$SERVICE.service" >/dev/null 2>&1 || true
rm -f /etc/systemd/system/$SERVICE.service
systemctl daemon-reload

if command -v nmcli >/dev/null 2>&1 && nmcli -t -f NAME connection show 2>/dev/null | grep -qx projector-link; then
  echo "==> Removing the projector Ethernet link"
  nmcli connection delete projector-link >/dev/null 2>&1 || true
fi

for f in /boot/firmware/cmdline.txt /boot/cmdline.txt /boot/firmware/config.txt /boot/config.txt; do
  if [[ -f "$f.balcony-backup" ]]; then
    echo "==> Restoring $f"
    cp "$f.balcony-backup" "$f"
    rm -f "$f.balcony-backup"
  fi
done
systemctl enable getty@tty1.service >/dev/null 2>&1 || true

echo
echo "Removed. mpv and python3-flask are still installed (apt-get remove them if you like)."
echo "Your videos are still in ~/media."
