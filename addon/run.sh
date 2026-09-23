#!/usr/bin/with-contenv bashio
set -e

MQTT_USERNAME="$(bashio::config 'mqtt_username')"
MQTT_PASSWORD="$(bashio::config 'mqtt_password')"

# The local Mosquitto app publishes short-lived credentials through the
# Supervisor service API. Prefer explicit options only when the user supplied
# them, otherwise consume those managed credentials automatically.
if bashio::services.available 'mqtt'; then
  if [[ -z "${MQTT_USERNAME}" ]]; then
    MQTT_USERNAME="$(bashio::services mqtt 'username')"
  fi
  if [[ -z "${MQTT_PASSWORD}" ]]; then
    MQTT_PASSWORD="$(bashio::services mqtt 'password')"
  fi
fi

export TC002_MQTT_USERNAME="${MQTT_USERNAME}"
export TC002_MQTT_PASSWORD="${MQTT_PASSWORD}"
export TC002_ADAPTER_TOKEN="$(bashio::config 'adapter_token')"

python3 -m tc002_awtrix_bridge.migrate_data \
  --source /config/tc002-migration --target /data

python3 -m tc002_awtrix_bridge.addon_config \
  --options /data/options.json \
  --output /data/bridge-config.yaml

exec tc002-awtrix-bridge --config /data/bridge-config.yaml
