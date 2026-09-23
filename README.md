# TC002 AWTRIX Bridge — Home Assistant add-on repository

This repository publishes the Home Assistant add-on independently from the
development repository. It contains no device configuration, credentials,
backups, or personal app data. The `addon/` directory is the installable app.

Add `https://github.com/lucaamo/tc002-awtrix-ha-addon` in Home Assistant's
**Settings → Apps → Install app → Repositories**, then install **TC002 AWTRIX
Bridge**. Enter the TC002 address and adapter mode in the app options. For a
new installation, follow [the add-on guide](addon/README.md).

The Supervisor discovers new versions when this repository's `addon/config.yaml`
version changes. Enable app auto-update to install later published releases.
Only qualified releases should be published here; development branches are
not an update channel.

An existing **local** installation has a different Supervisor app identifier.
Do not uninstall or disable it until its options and `/data` content have been
copied to the repository-installed app and the new app has passed a live check.
The two apps cannot run together on the same TCP/UDP ports and MQTT identity.

Release 0.2.25 is based on development commit `7f43e65cfd6bb9cdc8b4678d20defc41e87ba8bb`.
The distribution defaults use generic values; an existing installation's
options must be migrated separately.
