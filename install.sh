#!/bin/bash
# Installiert die Womo-Zentrale auf einem Raspberry Pi (Raspberry Pi OS mit Desktop, 64 Bit).
# Aufruf im Ordner womo-zentrale:   bash install.sh
set -e
DIR="$(cd "$(dirname "$0")" && pwd)"
[ -f "$DIR/config.json" ] || cp "$DIR/config.example.json" "$DIR/config.json"
rm -f "$DIR/.needs_install"
USERNAME="$(whoami)"

echo "== Pakete installieren"
sudo apt-get update
sudo apt-get install -y python3-aiohttp python3-paho-mqtt python3-bleak bluez wlr-randr || \
  sudo apt-get install -y python3-aiohttp python3-paho-mqtt bluez
sudo apt-get install -y git || true
sudo apt-get install -y python3-smbus2 i2c-tools || sudo apt-get install -y python3-smbus i2c-tools
pip3 install --break-system-packages bleak litime-ble greeclimate || echo "Hinweis: Python-Pakete konnten nicht vollständig installiert werden"

echo "== I2C für den Lagesensor einschalten"
sudo raspi-config nonint do_i2c 0 || true

echo "== 3D-Bibliothek lokal speichern"
mkdir -p "$DIR/static/vendor"
curl -fsSL -o "$DIR/static/vendor/three.min.js" https://cdnjs.cloudflare.com/ajax/libs/three.js/r128/three.min.js || \
  echo "three.js konnte nicht geladen werden – wird dann beim Start aus dem Internet geholt"
sudo usermod -aG bluetooth "$USERNAME"

echo "== Dienst einrichten (startet automatisch beim Einschalten)"
sudo tee /etc/systemd/system/womo-zentrale.service >/dev/null <<EOF
[Unit]
Description=Womo-Zentrale
After=network-online.target bluetooth.target
Wants=network-online.target

[Service]
User=$USERNAME
WorkingDirectory=$DIR
ExecStart=/usr/bin/python3 $DIR/server.py
Restart=always
RestartSec=5

[Install]
WantedBy=multi-user.target
EOF
sudo systemctl daemon-reload
sudo systemctl enable --now womo-zentrale

echo "== Helligkeit über die Oberfläche erlauben"
echo 'SUBSYSTEM=="backlight", RUN+="/bin/chmod 666 /sys/class/backlight/%k/brightness"' | \
  sudo tee /etc/udev/rules.d/99-womo-backlight.rules >/dev/null
sudo udevadm control --reload-rules && sudo udevadm trigger --subsystem-match=backlight || true

echo "== Neustart-Taste der Zentrale erlauben"
echo "$USERNAME ALL=(root) NOPASSWD: /usr/sbin/reboot, /sbin/reboot" | sudo tee /etc/sudoers.d/womo-zentrale >/dev/null
sudo chmod 440 /etc/sudoers.d/womo-zentrale

echo "== Bildschirmschoner aus"
sudo raspi-config nonint do_blanking 1 || true

echo "== Vollbild-Browser beim Start"
BROWSER="$(command -v chromium || command -v chromium-browser)"
sudo apt-get install -y wtype xdotool || true
KIOSK="$BROWSER --start-fullscreen --noerrdialogs --disable-infobars --no-first-run --password-store=basic --autoplay-policy=no-user-gesture-required --app=http://localhost:8080"
mkdir -p ~/.config/labwc
touch ~/.config/labwc/autostart
sed -i '/localhost:8080/d' ~/.config/labwc/autostart
echo "sleep 4; $KIOSK &" >> ~/.config/labwc/autostart
if [ -f ~/.config/wayfire.ini ]; then
  sed -i '/localhost:8080/d' ~/.config/wayfire.ini
  grep -q "^\[autostart\]" ~/.config/wayfire.ini || printf "\n[autostart]\n" >> ~/.config/wayfire.ini
  sed -i "/^\[autostart\]/a womo = sleep 4; $KIOSK" ~/.config/wayfire.ini
fi

echo
echo "Fertig. Jetzt config.json anpassen (Cerbo-Adresse, ggf. Passwort) und neu starten: sudo reboot"
