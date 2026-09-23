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

Release 0.2.30 adds an optional twelve-hour dry forecast visibility switch to
the 52×16 rain page. Source change: private development commit `60e0649`.
It is based on development commit `ee5763d`.

The repository also includes a one-time importer for existing local installations.
Its snapshot archive must be staged in the repository app's private `addon_config`
directory with a SHA-256 manifest before first start. It imports only the
known bridge state files and icons, never Supervisor options. Keep a backup
and the local app installed until the new app is verified. The distribution
defaults use generic values; existing options must be migrated separately.
