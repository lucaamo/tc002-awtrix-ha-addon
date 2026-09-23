# Changelog

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
