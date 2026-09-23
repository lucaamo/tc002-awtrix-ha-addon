# TC002 AWTRIX Bridge add-on

Self-contained Home Assistant add-on build context. Configure the TC002 address,
MQTT broker/prefix and matching adapter token in the add-on options. The full
behavior, security and installation guide is in the repository `docs` folder.

Open **Web UI** from the add-on page for the live 52x16 preview, carousel
controls and App Studio. App Studio creates persistent static, sensor,
countdown, weather, rain-chart, calendar and Todo pages with advanced colours,
thresholds, effects, lifetime and visibility controls without editing YAML.
Each app can use the global layout, an adaptive large reading, or a fixed title
above its value; the three choices have live 52x16 previews before saving.
The dashboard is available in Italian and English. It starts from the browser
language and remembers an explicit IT/EN selection locally.
AWTRIX Tools add notifications, moodlight and indicators. LaMetric gallery icons can be imported by their
numeric ID as a faithful 16x16 asset or a symmetric 12x16 TC002-optimized asset
that leaves four more columns for text. Switching mode replaces the same icon
entry instead of creating a duplicate.

The **Modalità Sonos** section configures a Home Assistant media player,
playlist URI, volume step and long-press threshold. With the native companion,
the knob and rocker become play/pause, next/previous and Sonos volume controls
while a dedicated now-playing screen pauses the normal carousel.

## 0.2.29

Rain forecasts now use a dedicated 52×16 timeline with an arrival-time
headline, amount bars or line, heavy-rain colour, dry outlook and probability
fallback. The preview uses the same renderer as the live panel.

## 0.2.28

Reports the installed package version in health, device info and discovery.

## 0.2.27

Adds a one-time, checksum-verified import path for migration from the local
add-on. The new repository add-on reads a prepared snapshot from its own
read-only `/config/tc002-migration.tar.gz` archive, imports only the known state
files and icons into a fresh `/data`, and refuses to overwrite existing state.
The snapshot and manifest are never distributed with the add-on. Supervisor
options, including the adapter token and MQTT settings, must be transferred
separately. Remove the staged snapshot after a successful migration.

## 0.2.25

Adds a device settings overview with live adapter/MQTT status and direct access
to existing bridge controls. Wi-Fi and future standalone runtime settings are
clearly identified as unavailable in the current add-on; no device firmware or
network configuration changes are made.

## 0.2.24

Connects the project link on the Home Assistant add-on information card to the
public GitHub repository.
