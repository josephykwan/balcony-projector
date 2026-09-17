#!/usr/bin/env bash
# Sets up the Balcony Projector on a Raspberry Pi running Raspberry Pi OS Lite.
#
# Run it from the folder that holds app.py, on the Pi:
#
#   sudo ./install.sh [--quiet-boot] [--projector-link] [--timezone America/Chicago]
#
#   --quiet-boot      Hide the boot text, login prompt and rainbow splash so the
#                     street only ever sees black or video. Backs up cmdline.txt.
#   --projector-link  Give the Ethernet port the fixed address 192.168.50.1 for a
#                     direct cable to the projector (which is set to 192.168.50.2).
#   --timezone ZONE   Set the Pi's clock zone, so the evening schedule is right.
#
# Safe to run again; it only adds what is missing.

set -euo pipefail

if [[ $EUID -ne 0 ]]; then
  echo "Run this with sudo:  sudo ./install.sh"
  exit 1
fi

APP_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
RUN_USER="${SUDO_USER:-pi}"
RUN_HOME="$(getent passwd "$RUN_USER" | cut -d: -f6)"
SERVICE=balcony-projector
QUIET_BOOT=0
PROJECTOR_LINK=0
TIMEZONE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quiet-boot) QUIET_BOOT=1 ;;
    --projector-link) PROJECTOR_LINK=1 ;;
    --timezone) TIMEZONE="${2:-}"; shift ;;
    -h|--help) sed -n '2,15p' "$0"; exit 0 ;;
    *) echo "Unknown option: $1"; exit 1 ;;
  esac
  shift
done

if [[ -z "$RUN_HOME" || ! -d "$RUN_HOME" ]]; then
  echo "Can't find a home folder for user $RUN_USER."
  exit 1
fi

echo "==> Installing mpv and Flask"
apt-get update -qq
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-flask mpv

echo "==> Letting $RUN_USER use the display and sound"
for grp in video render audio input; do
  getent group "$grp" >/dev/null && usermod -aG "$grp" "$RUN_USER"
done

echo "==> Making media folders in $RUN_HOME/media"
for folder in halloween campaign movies; do
  install -d -o "$RUN_USER" -g "$RUN_USER" "$RUN_HOME/media/$folder"
done
chown -R "$RUN_USER:$RUN_USER" "$APP_DIR"

if [[ -n "$TIMEZONE" ]]; then
  echo "==> Setting the time zone to $TIMEZONE"
  timedatectl set-timezone "$TIMEZONE"
fi

if [[ $PROJECTOR_LINK -eq 1 ]]; then
  echo "==> Setting the Ethernet port to 192.168.50.1 for the projector cable"
  if command -v nmcli >/dev/null 2>&1; then
    nmcli connection delete projector-link >/dev/null 2>&1 || true
    nmcli connection add type ethernet ifname eth0 con-name projector-link \
      ipv4.method manual ipv4.addresses 192.168.50.1/24 ipv4.never-default yes \
      ipv6.method disabled connection.autoconnect yes >/dev/null
    nmcli connection up projector-link >/dev/null 2>&1 || echo "    (cable not plugged in yet; it will connect when it is)"
  else
    echo "    NetworkManager isn't here. Give eth0 the address 192.168.50.1/24 by hand."
  fi
fi

if [[ $QUIET_BOOT -eq 1 ]]; then
  echo "==> Hiding boot text and the login prompt on the projector"
  CMDLINE=/boot/firmware/cmdline.txt
  [[ -f $CMDLINE ]] || CMDLINE=/boot/cmdline.txt
  CONFIG=/boot/firmware/config.txt
  [[ -f $CONFIG ]] || CONFIG=/boot/config.txt
  if [[ -f $CMDLINE ]]; then
    [[ -f $CMDLINE.balcony-backup ]] || cp "$CMDLINE" "$CMDLINE.balcony-backup"
    line="$(tr -d '\n' < "$CMDLINE")"
    line="${line//console=tty1/console=tty3}"
    for opt in quiet loglevel=0 logo.nologo vt.global_cursor_default=0 consoleblank=0 plymouth.ignore-serial-consoles; do
      [[ " $line " == *" $opt "* ]] || line="$line $opt"
    done
    printf '%s\n' "$line" > "$CMDLINE"
  fi
  if [[ -f $CONFIG ]]; then
    [[ -f $CONFIG.balcony-backup ]] || cp "$CONFIG" "$CONFIG.balcony-backup"
    grep -q '^disable_splash=1' "$CONFIG" || printf '\n# balcony-projector: no rainbow splash\ndisable_splash=1\n' >> "$CONFIG"
  fi
  systemctl disable getty@tty1.service >/dev/null 2>&1 || true
fi

echo "==> Installing the $SERVICE service"
cat > /etc/systemd/system/$SERVICE.service <<EOF
[Unit]
Description=Balcony Projector (video loop and phone remote)
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=$RUN_USER
Group=$RUN_USER
SupplementaryGroups=video render audio input
WorkingDirectory=$APP_DIR
ExecStart=/usr/bin/python3 $APP_DIR/app.py
Restart=always
RestartSec=3
Environment=PYTHONUNBUFFERED=1
Environment=HOME=$RUN_HOME
StandardOutput=journal
StandardError=journal

[Install]
WantedBy=multi-user.target
EOF

systemctl daemon-reload
systemctl enable "$SERVICE.service" >/dev/null
systemctl restart "$SERVICE.service"

PORT="$(python3 -c "import json;print(json.load(open('$APP_DIR/config.json')).get('port',8080))" 2>/dev/null || echo 8080)"
echo
echo "Done. On your phone, open:  http://$(hostname).local:$PORT/"
echo "Videos go in:  $RUN_HOME/media/halloween, campaign, movies"
echo "Logs:          journalctl -u $SERVICE -f"
if [[ $QUIET_BOOT -eq 1 ]]; then
  echo "Reboot once for the quiet boot to take effect:  sudo reboot"
fi
