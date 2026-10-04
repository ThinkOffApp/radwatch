#!/bin/bash
# radwatch installer: puts the RadiaCode logger on any Debian-ish box with Python 3.11+.
# Usage:  ./install.sh            installs, does not start (no device yet)
#         ./install.sh --with-mqtt   also installs a loopback-only mosquitto broker
#
# Deliberately NOT started on install: the unit stays disabled until you put the device's
# serial or Bluetooth MAC in /etc/default/radwatch. A logger that starts without a device
# just fills the journal with connection errors.
set -eu
WITH_MQTT=0; [ "${1:-}" = "--with-mqtt" ] && WITH_MQTT=1
U=$(id -un); H=$HOME/radwatch
echo "installing radwatch for $U into $H"
mkdir -p "$H"
install -m 0755 "$(dirname "$0")/radwatch.py" "$H/radwatch.py"
# Debian ships python3 without ensurepip, so venv creation fails on a fresh box. Caught by
# test-installing on a second machine: the Pi had python3-venv, the Ventuno did not, and the
# original `|| true` turned that into a confusing pip-not-found error three lines later.
if ! python3 -c 'import ensurepip' >/dev/null 2>&1; then
  echo "--- python3-venv is missing (no ensurepip)"
  if sudo -n true 2>/dev/null; then
    sudo apt-get install -y -qq python3-venv >/dev/null 2>&1 \
      || sudo apt-get install -y -qq "python3.$(python3 -c 'import sys;print(sys.version_info[1])')-venv" >/dev/null 2>&1 \
      || { echo "apt could not install python3-venv on this box"; exit 1; }
  else
    # Say WHICH thing is missing. "could not install" sent me looking for a broken package
    # when the real answer was that sudo wanted a password on this machine and not on the last one.
    cat <<MSG
This box needs python3-venv and sudo here asks for a password, so I cannot install it.
Run this once, then re-run me:

  sudo apt-get install -y python3-venv

MSG
    exit 1
  fi
fi
python3 -m venv "$H/venv" || { echo "venv creation failed in $H/venv"; exit 1; }
"$H/venv/bin/pip" install -q --upgrade pip >/dev/null 2>&1 || true
"$H/venv/bin/pip" install -q radiacode paho-mqtt || { echo "pip install failed"; exit 1; }
echo "--- self test (no hardware needed):"
"$H/venv/bin/python" "$H/radwatch.py" selftest || { echo "SELFTEST FAILED, stopping"; exit 1; }

if [ $WITH_MQTT = 1 ]; then
  echo "--- mosquitto, bound to loopback only"
  sudo apt-get install -y -qq mosquitto mosquitto-clients >/dev/null
  printf 'listener 1883 127.0.0.1\nallow_anonymous true\n' | sudo tee /etc/mosquitto/conf.d/local-only.conf >/dev/null
  sudo systemctl enable --now mosquitto
fi

echo "--- systemd unit (installed disabled)"
sudo tee /etc/systemd/system/radwatch.service >/dev/null <<EOF
[Unit]
Description=radwatch (RadiaCode logger -> SQLite + Home Assistant over MQTT)
After=network-online.target mosquitto.service
Wants=network-online.target

[Service]
Type=simple
User=$U
WorkingDirectory=$H
EnvironmentFile=-/etc/default/radwatch
ExecStart=$H/venv/bin/python $H/radwatch.py log --db $H/radwatch.sqlite --mqtt \$RADWATCH_ARGS
Restart=on-failure
RestartSec=20

[Install]
WantedBy=multi-user.target
EOF
[ -f /etc/default/radwatch ] || printf '# RADWATCH_ARGS=--serial RC-10x-XXXXXX\n# RADWATCH_ARGS=--bt AA:BB:CC:DD:EE:FF\nRADWATCH_ARGS=\n' | sudo tee /etc/default/radwatch >/dev/null
sudo systemctl daemon-reload
cat <<EOF

installed, not started.

next:
  1. put the device serial or BT MAC in /etc/default/radwatch
  2. sudo systemctl enable --now radwatch
  3. journalctl -u radwatch -f

Home Assistant finds it by itself over MQTT discovery, no custom component.
EOF
