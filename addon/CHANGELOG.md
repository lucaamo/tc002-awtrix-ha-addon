# Changelog

## 0.2.46

Add an AWTRIX NG option that keeps App Studio synchronization active while
disabling and removing the legacy aggregate `awtrix_bridge` pushed app. This
prevents a Studio page such as CO2 from appearing twice when NG already owns
the carousel.

## 0.2.45

Insert newly published Studio apps into the AWTRIX NG carousel order during the
same synchronization pass. This covers both direct upgrades from hashed names
and Studio apps created after migration, without moving existing native apps.

## 0.2.44

Complete the readable App Studio migration by removing obsolete hashed names
from AWTRIX NG's saved carousel order as well as deleting their pushed content.
This prevents empty legacy rows from remaining in the Apps page after upgrade.

## 0.2.43

App Studio now publishes readable AWTRIX NG identifiers such as `Studio_CO2`,
`Studio_Meteo` and `Studio_Compleanno`. Unicode titles are converted to valid
device identifiers, long names respect the 32-character limit, and only true
name collisions receive a short suffix. The first synchronization removes the
legacy `tc002studio_<hash>` pushed apps after their readable replacements are
published.

Add the persistent 52×16 CasaViva Berry app and its migration guide. CasaViva
runs in the AWTRIX NG carousel and consumes the existing compact MQTT state,
so the old bridge-rendered page can be removed without moving credentials to
the device script.

## 0.2.42

AWTRIX NG Sonos Remote can send the selected Home Assistant `media_player`
with every command. The bridge validates the entity, executes play/pause,
track, volume and media-start services for that player, and returns its own
artist, title, playback state, volume and friendly name over an isolated v2
MQTT topic. Legacy plain commands remain accepted for compatibility.
New installations start without a deployment-specific player entity; existing
saved selections are preserved.

## 0.2.41

App Studio now exports every enabled page as its own volatile AWTRIX NG app
when `adapter_mode` is `awtrix_ng_mqtt`. The add-on reconciles additions,
updates, removals and Studio ordering over the device HTTP API while the
existing MQTT adapter remains available for bridge notifications and legacy
pages. Animated Studio pages refresh at the configured HTTP frame rate only
while visible; inactive pages use low-rate lifetime refreshes.

The Studio UI reports AWTRIX NG connection and per-app synchronization state,
offers manual synchronization, and routes **Show** and **Save and show** to the
physical NG carousel. `switchOnChange` also selects the exported page when its
rendered content changes. Exported Studio pages are skipped inside the legacy
aggregate bridge page so each appears only once in the NG carousel.

New-install defaults no longer contain a deployment-specific UID, display
name, MQTT prefix or private-network host address. Existing Home Assistant
options are preserved during upgrades.

## 0.2.40

The AWTRIX NG MQTT bridge app now remains in NG's normal carousel for ten
seconds, then yields to the next app. This restores access to Berry apps such
as Sonos Remote while the bridge continues rendering its own pages.

## 0.2.39

Added an AWTRIX NG MQTT display adapter for TC002. It publishes complete 52×16
bridge frames to a pushed app through the configured broker, selects the app
when NG comes online, and removes it when the bridge stops. The bridge uses
NG's retained availability topic for device health and does not depend on the
NG HTTP API. Set `adapter_mode` to `awtrix_ng_mqtt` and
`tc002_ng_mqtt_prefix` to the prefix configured on NG.

## 0.2.38

- Let App Studio choose one carousel timing rule per app: elapsed seconds or a
  number of complete text scrolls. Existing apps keep their seconds-based rule.
- In scroll mode, short text scrolls too. The bridge waits for the selected
  count before changing pages, with a ten-minute safety deadline. Rain charts,
  compact layouts and apps with scrolling disabled use seconds.

## 0.2.37

- Use one fixed age-aware birthday message per language (Italian or English),
  selected in App Studio. Remove per-person message editing and export.
- Change the sample CSV to `name,date`; accept older three-column CSV files
  while discarding their message column. Discard previously saved custom
  messages during migration. Require a birth year before showing the age.

## 0.2.36

- Store full birth dates for Birthday apps and calculate the age on the matching
  day in the bridge time zone. Messages support `{name}`, `{age}` and
  `{ageUnit}` (Italian singular/plural); the default states the age explicitly.
- Require a full birth date for new manual and CSV entries. Previously saved
  month/day-only entries remain readable, but need a year for age messages.
- Update the downloadable CSV example and use a longer default display time
  for birthday messages.

## 0.2.35

- Give Birthday list actions full-size buttons and a responsive layout so
  adding a person and uploading CSV remain legible in Home Assistant.
- Add a downloadable example CSV directly in App Studio.

## 0.2.34

- Keep the Python package version and Home Assistant add-on version in sync
  so health and device status report the installed release accurately.

## 0.2.33

- Add a Birthday source to App Studio. Enter people and personalized messages
  manually or import a local UTF-8 CSV with `name,date,message` columns.
- Store birthday lists only in the add-on's private app state; show a birthday
  page only on matching days in the bridge time zone. Rotate multiple messages
  on the same day once per minute. February 29 matches only in leap years.

## 0.2.32

- Improve App Studio in Home Assistant's narrower viewport: put the library
  above the editor and hide controls that do not affect rain or compact pages.
- Keep the preview caption consistent with the selected sample/current-data mode.


## 0.2.31

- Rework App Studio as a guided editor with source cards, presets, type-specific
  compact 52×16 layouts, a searchable library, duplicate/rename, and reviewed
  import/export. Existing app definitions keep their prior layout.
- Add separate sample and current-data previews, animation playback, entity
  suggestions, clearer visibility states, screen-reader labels and long-text
  warnings. Correct countdown defaults to the bridge time zone.
- Refresh sensors and countdowns every two seconds; cache weather, rain,
  calendar and Todo data for a minute between normal polling cycles.


## 0.2.30

- Add a Rain app switch to hide its page after twelve dry hourly forecasts and
  restore it automatically when rain is expected. Existing apps remain visible
  by default; incomplete forecast data never hides the page.

## 0.2.29

- Redesigned rain forecasts for the TC002's 52×16 panel: rain arrival, a
  ten-hour timeline, intensity colours, dry outlook and probability fallback.
- Rain preview now uses the same pixel layout as the live display.
- Added this changelog to Home Assistant's update dialog.

## 0.2.28

- Report the installed package version in health and device status.

## 0.2.27

- Import verified state and icons when moving from a local add-on installation.
