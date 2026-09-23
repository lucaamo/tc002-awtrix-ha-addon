# TC002 AWTRIX Bridge add-on

Self-contained Home Assistant add-on build context. Configure the TC002 address,
MQTT broker/prefix and matching adapter token in the add-on options. The full
behavior, security and installation guide is in the repository `docs` folder.

Open **Web UI** from the add-on page for the live 52x16 preview, carousel
controls and App Studio. App Studio creates persistent static, sensor,
countdown, weather, rain-chart, calendar, Todo and birthday pages with advanced colours,
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

## 0.2.38

App Studio now asks when to move to the next app: after a number of seconds or
after a number of complete text scrolls. Only the chosen control appears.
Existing apps continue using seconds until changed. Scroll timing forces even
short text to move; a ten-minute safety deadline prevents a page getting stuck.
Rain charts, compact layouts and apps with scrolling disabled use seconds.

## 0.2.37

Birthday entries contain only a name and full birth date (`YYYY-MM-DD`). App
Studio uses one fixed message for everyone, in the selected Italian or English
language: “Oggi è il compleanno di Nome. Compie XX anni!” / “Today is Name's
birthday. Name turns XX years old!” Download a two-column `name,date` CSV
example in App Studio. Older CSV files with a `message` column remain importable,
but those messages are ignored; saved custom birthday messages are removed when
the app starts. A saved month/day-only entry needs a birth year before it can
display an age. Birth dates remain in private add-on state; Studio JSON exports
include them and should be kept private.

## 0.2.36

Birthday entries now take a full birth date (`YYYY-MM-DD`), so `{age}` can show
the age reached on that birthday. `{ageUnit}` renders `anno` for one year and
`anni` otherwise. With an empty custom message, the display says “Oggi è il
compleanno di {name}. Compie {age} {ageUnit}!” Birthday screens default to 45
seconds so long messages can scroll across the 52×16 panel. The sample CSV
uses full birth dates. Existing month/day-only entries continue to display
their old messages until you add the year; editing those entries requires the
full date. Birth dates stay in the add-on's private state, but Studio JSON
exports contain them and should be kept private.

## 0.2.35

Birthday list controls are easier to read in Home Assistant. Use **Scarica CSV
di esempio** in App Studio to download a sample file, replace the generic
names/dates/messages locally, then use **Carica CSV**. The example contains no
personal data.

## 0.2.34

The health and device endpoints now report the same installed version shown
by Home Assistant.

## 0.2.33

App Studio includes a Birthday source. Add people and personalized messages
manually or upload a local UTF-8 CSV with the header `name,date,message`.
This first release stored month and day only; 0.2.36 adds the birth year and
age calculation. `{name}` inserts each person's name. The app enters the carousel
only on a matching date in the bridge time zone; multiple matching messages
rotate once per minute. February 29 appears only in leap years. Lists live in
the add-on's private state, never in this repository. A Studio JSON export
*does* include the birthday list, so keep exports private.

## 0.2.32

The Studio library stacks above the editor in narrower Home Assistant windows.
Rain and compact layouts hide controls that do not affect their pixels, and the
preview caption follows the selected sample/current-data mode.

## 0.2.31

App Studio now starts with source cards and presets, groups advanced controls,
and offers both sample and current-data previews. Weather, calendar and Todo
can use a dedicated two-line TC002 layout on new apps; existing definitions
retain their prior appearance. Search, duplicate, rename and JSON export/import
manage the library. Exported files contain your entity IDs and app content, so
store them privately. Dynamic-source polling follows source-specific intervals.

## 0.2.30

The Rain app can optionally leave the carousel when all twelve upcoming hourly
forecasts are dry. It returns automatically when rain is expected; incomplete
forecasts leave it visible. Existing Rain apps keep showing until this switch is
turned off in App Studio.

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
