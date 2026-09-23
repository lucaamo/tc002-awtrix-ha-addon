# Changelog

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
