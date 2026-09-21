#!/usr/bin/env bash
# Sets up the Balcony Projector on a Raspberry Pi running Raspberry Pi OS Lite.
#
# Run it from the folder that holds app.py, on the Pi:
#
#   sudo ./install.sh [--quiet-boot] [--share] [--projector-link] [--timezone America/Chicago]
#
#   --quiet-boot      Hide the boot text, login prompt and rainbow splash so the
#                     street only ever sees black or video. Backs up cmdline.txt.
#   --share           Share ~/media on the Wi-Fi (Samba) so it shows up as a
#                     network drive on a Mac or Windows laptop.
#   --projector-link  Only for projectors with a LAN port: give the Ethernet port
#                     the fixed address 192.168.50.1 for a direct cable.
#   --timezone ZONE   Set the Pi's clock zone, so the evening schedule is right.
#
# Always sets the HDMI output to the projector's native 1280x800.
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
SHARE=0
PROJECTOR_LINK=0
TIMEZONE=""

while [[ $# -gt 0 ]]; do
  case "$1" in
    --quiet-boot) QUIET_BOOT=1 ;;
    --share) SHARE=1 ;;
    --projector-link) PROJECTOR_LINK=1 ;;
    --timezone) TIMEZONE="${2:-}"; shift ;;
    -h|--help) sed -n '2,18p' "$0"; exit 0 ;;
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
DEBIAN_FRONTEND=noninteractive apt-get install -y -qq python3-flask mpv openssl

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

CMDLINE=/boot/firmware/cmdline.txt
[[ -f $CMDLINE ]] || CMDLINE=/boot/cmdline.txt
CONFIG=/boot/firmware/config.txt
[[ -f $CONFIG ]] || CONFIG=/boot/config.txt

add_cmdline_opts() {
  # cmdline.txt is a single line; add each option once
  [[ -f $CMDLINE ]] || return 0
  [[ -f $CMDLINE.balcony-backup ]] || cp "$CMDLINE" "$CMDLINE.balcony-backup"
  local line
  line="$(tr -d '\n' < "$CMDLINE")"
  for opt in "$@"; do
    [[ " $line " == *" $opt "* ]] || line="$line $opt"
  done
  printf '%s\n' "$line" > "$CMDLINE"
}

echo "==> Setting the HDMI output to the projector's native 1280x800 (kept on even when the projector is off)"
if [[ -f $CMDLINE ]] && grep -q "video=HDMI-A-1:" "$CMDLINE"; then
  echo "    (a video= setting is already there, leaving it)"
else
  add_cmdline_opts "video=HDMI-A-1:1280x800@60D"
fi

if [[ $SHARE -eq 1 ]]; then
  echo "==> Sharing $RUN_HOME/media on the network"
  DEBIAN_FRONTEND=noninteractive apt-get install -y -qq samba
  if ! grep -q "balcony-projector share" /etc/samba/smb.conf; then
    cat >> /etc/samba/smb.conf <<EOF

# balcony-projector share (start)
[media]
   comment = Balcony projector videos
   path = $RUN_HOME/media
   browseable = yes
   writable = yes
   guest ok = yes
   force user = $RUN_USER
   create mask = 0664
   directory mask = 0775
# balcony-projector share (end)
EOF
  fi
  sed -i 's/^\(\s*\)map to guest = .*/\1map to guest = Bad User/' /etc/samba/smb.conf
  grep -q "map to guest" /etc/samba/smb.conf || sed -i '/^\[global\]/a\   map to guest = Bad User' /etc/samba/smb.conf
  systemctl enable smbd >/dev/null 2>&1 || true
  systemctl restart smbd
fi

if [[ $QUIET_BOOT -eq 1 ]]; then
  echo "==> Hiding boot text and the login prompt on the projector"
  if [[ -f $CMDLINE ]]; then
    line="$(tr -d '\n' < "$CMDLINE")"
    printf '%s\n' "${line//console=tty1/console=tty3}" > "$CMDLINE"
    add_cmdline_opts quiet loglevel=0 logo.nologo vt.global_cursor_default=0 consoleblank=0 plymouth.ignore-serial-consoles
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
echo "Studio (laptop):  https://$(hostname).local:8443/studio/   (accept the certificate warning once)"
echo "Videos go in:  $RUN_HOME/media/halloween, campaign, movies"
if [[ $SHARE -eq 1 ]]; then
  echo "Network drive: smb://$(hostname).local/media  (Mac: Finder > Go > Connect to Server, connect as Guest)"
fi
echo "Logs:          journalctl -u $SERVICE -f"
echo "Reboot once so the screen resolution (and quiet boot) take effect:  sudo reboot"
