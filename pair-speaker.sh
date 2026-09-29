#!/usr/bin/env bash
# Pair a Bluetooth speaker with the Pi so the videos' sound plays through it.
#
#   ./pair-speaker.sh              scan, then pick the speaker from a list
#   ./pair-speaker.sh "JBL Flip"   pick the first speaker whose name contains this
#   ./pair-speaker.sh --status     show what is paired and connected
#   ./pair-speaker.sh --forget     unpair the speaker again
#
# Needs the sound server from  sudo ./install.sh --bluetooth . Run it as the
# normal user (not sudo). Put the speaker in pairing mode first: usually hold its
# Bluetooth button until the light blinks.

set -uo pipefail

if [[ $EUID -eq 0 ]]; then
  echo "Run this without sudo, as the user who runs the player."
  exit 1
fi
export XDG_RUNTIME_DIR="${XDG_RUNTIME_DIR:-/run/user/$(id -u)}"

if ! command -v bluetoothctl >/dev/null; then
  echo "Bluetooth is not installed. Run:  sudo ./install.sh --bluetooth"
  exit 1
fi
if [[ ! -S "$XDG_RUNTIME_DIR/pipewire-0" ]]; then
  echo "The sound server is not running. Run  sudo ./install.sh --bluetooth  and reboot, then try again."
  exit 1
fi

speakers() {
  # paired devices that are audio sinks
  bluetoothctl devices Paired 2>/dev/null | while read -r _ mac name; do
    if bluetoothctl info "$mac" 2>/dev/null | grep -q "Audio Sink"; then
      echo "$mac $name"
    fi
  done
}

status() {
  local found=0
  while read -r mac name; do
    [[ -z "${mac:-}" ]] && continue
    found=1
    if bluetoothctl info "$mac" | grep -q "Connected: yes"; then
      echo "Connected:  $name ($mac)"
    else
      echo "Paired but not connected:  $name ($mac).  Turn it on; it should reconnect by itself."
    fi
  done < <(speakers)
  [[ $found -eq 0 ]] && echo "No speaker is paired yet."
  if command -v wpctl >/dev/null; then
    echo "Sound is going to: $(wpctl inspect @DEFAULT_AUDIO_SINK@ 2>/dev/null | sed -n 's/.*node.description = "\(.*\)"/\1/p' | head -1)"
  fi
}

case "${1:-}" in
  --status) status; exit 0 ;;
  --forget)
    while read -r mac name; do
      [[ -z "${mac:-}" ]] && continue
      echo "Forgetting $name"
      bluetoothctl remove "$mac" >/dev/null
    done < <(speakers)
    exit 0 ;;
esac

WANT="${1:-}"
bluetoothctl power on >/dev/null
bluetoothctl agent NoInputNoOutput >/dev/null 2>&1 || true

echo "Looking for speakers for 20 seconds. Make sure the speaker is in pairing mode (blinking light)..."
timeout 22 bluetoothctl --timeout 20 scan on >/dev/null 2>&1 || true

mapfile -t FOUND < <(bluetoothctl devices | awk '{mac=$2; $1=""; $2=""; sub(/^  /, ""); if ($0 != "" && $0 !~ /^([0-9A-F]{2}-){5}[0-9A-F]{2}$/) print mac "|" $0}')
if [[ ${#FOUND[@]} -eq 0 ]]; then
  echo "Nothing found. Is the speaker in pairing mode and within a few feet of the Pi? Try again."
  exit 1
fi

PICK=""
if [[ -n "$WANT" ]]; then
  for entry in "${FOUND[@]}"; do
    name="${entry#*|}"
    if [[ "${name,,}" == *"${WANT,,}"* ]]; then PICK="$entry"; break; fi
  done
  if [[ -z "$PICK" ]]; then
    echo "No device called \"$WANT\". Found:"
    for entry in "${FOUND[@]}"; do echo "  ${entry#*|}"; done
    exit 1
  fi
else
  echo "Found:"
  i=1
  for entry in "${FOUND[@]}"; do echo "  $i) ${entry#*|}"; i=$((i+1)); done
  read -r -p "Which one is the speaker? (number, or Enter to give up) " n
  [[ "$n" =~ ^[0-9]+$ && $n -ge 1 && $n -le ${#FOUND[@]} ]] || { echo "Nothing changed."; exit 1; }
  PICK="${FOUND[$((n-1))]}"
fi

MAC="${PICK%%|*}"; NAME="${PICK#*|}"
echo "Pairing with $NAME..."
bluetoothctl pair "$MAC" 2>&1 | grep -Ei "pair|fail|error" | tail -2
bluetoothctl trust "$MAC" >/dev/null
for attempt in 1 2 3; do
  if bluetoothctl connect "$MAC" 2>&1 | grep -q "Connection successful"; then break; fi
  sleep 3
done
sleep 3
if bluetoothctl info "$MAC" | grep -q "Connected: yes"; then
  echo "Connected. The player now sends sound to $NAME whenever it is on."
  echo "On the phone page, Settings, \"Play sound through\" can stay on Automatic."
  status
else
  echo "Paired, but it did not connect. Turn the speaker off and on again; it should connect on its own."
  echo "If not, run  ./pair-speaker.sh --status  to check, or pair again."
  exit 1
fi
