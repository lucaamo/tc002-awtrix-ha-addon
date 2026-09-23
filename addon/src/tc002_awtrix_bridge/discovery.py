from __future__ import annotations

from typing import Any

from . import __version__
from .engine import Engine
from .validation import TRANSITIONS


def home_assistant_discovery(engine: Engine) -> dict[str, Any]:
    """Build the upstream-style single retained HA device discovery payload."""
    uid = engine.config.uid
    prefix = engine.config.mqtt.prefix
    components: dict[str, dict[str, Any]] = {
        "mat": {
            "~": prefix,
            "p": "light",
            "name": "Matrix",
            "ic": "mdi:clock-digital",
            "cmd_t": "~/cmd/display",
            "pl_on": '{"power":true}',
            "pl_off": '{"power":false}',
            "stat_t": "~/state/device",
            "stat_val_tpl": (
                '{% if value_json.matrixPower %}{"power":true}{% else %}{"power":false}{% endif %}'
            ),
            "bri_cmd_t": "~/cmd/settings",
            "bri_cmd_tpl": '{"brightness":{{ value }}}',
            "bri_stat_t": "~/state/device",
            "bri_val_tpl": "{{ value_json.brightness }}",
            "rgb_cmd_t": "~/cmd/settings",
            "rgb_cmd_tpl": '{"textColor":[{{ red }},{{ green }},{{ blue }}]}',
            "rgb_stat_t": "~/state/settings",
            "rgb_val_tpl": (
                "{{ value_json.textColor[1:3]|int(base=16) }},"
                "{{ value_json.textColor[3:5]|int(base=16) }},"
                "{{ value_json.textColor[5:7]|int(base=16) }}"
            ),
            "uniq_id": f"{uid}_mat",
        },
        "ind1": _indicator(uid, prefix, 1),
        "ind2": _indicator(uid, prefix, 2),
        "ind3": _indicator(uid, prefix, 3),
        "brimode": {
            "~": prefix,
            "p": "select",
            "name": "Brightness mode",
            "ic": "mdi:brightness-auto",
            "ops": ["Manual", "Auto"],
            "cmd_t": "~/cmd/settings",
            "cmd_tpl": "{\"autoBrightness\":{{ 'true' if value == 'Auto' else 'false' }}}",
            "stat_t": "~/state/settings",
            "val_tpl": "{{ 'Auto' if value_json.autoBrightness else 'Manual' }}",
            "uniq_id": f"{uid}_brimode",
        },
        "transeff": {
            "~": prefix,
            "p": "select",
            "name": "Transition effect",
            "ic": "mdi:auto-fix",
            "cmd_t": "~/cmd/settings",
            "cmd_tpl": '{"transitionEffect":"{{ value }}"}',
            "stat_t": "~/state/settings",
            "val_tpl": "{{ value_json.transitionEffect }}",
            "ops": TRANSITIONS,
            "uniq_id": f"{uid}_transeff",
        },
        "trans": {
            "~": prefix,
            "p": "switch",
            "name": "Transition",
            "ic": "mdi:swap-horizontal",
            "cmd_t": "~/cmd/settings",
            "pl_on": '{"autoTransition":true}',
            "pl_off": '{"autoTransition":false}',
            "stat_t": "~/state/settings",
            "val_tpl": "{{ 'ON' if value_json.autoTransition else 'OFF' }}",
            "stat_on": "ON",
            "stat_off": "OFF",
            "uniq_id": f"{uid}_trans",
        },
        "next": _button(uid, prefix, "next", "Next app", "apps/next", "mdi:arrow-right-bold"),
        "prev": _button(
            uid, prefix, "prev", "Previous app", "apps/previous", "mdi:arrow-left-bold"
        ),
        "dismiss": _button(
            uid, prefix, "dismiss", "Dismiss notification", "notify/dismiss", "mdi:bell-off"
        ),
        "app": _sensor(uid, prefix, "app", "Current app", "~/state/apps/active", None, "mdi:apps"),
        "ver": _sensor(
            uid, prefix, "ver", "Version", "~/state/device", "{{ value_json.version }}", "mdi:tag"
        ),
        "ip": _sensor(
            uid,
            prefix,
            "ip",
            "IP address",
            "~/state/device",
            "{{ value_json.ipAddress|default('') }}",
            "mdi:wifi",
        ),
        "prefix": _sensor(
            uid, prefix, "prefix", "MQTT prefix", "~/state/prefix", None, "mdi:tag-text"
        ),
        "rssi": _sensor(
            uid,
            prefix,
            "rssi",
            "WiFi strength",
            "~/state/device",
            "{{ value_json.wifiRssi|default(none) }}",
            device_class="signal_strength",
            unit="dBm",
        ),
        "uptime": _sensor(
            uid,
            prefix,
            "uptime",
            "Uptime",
            "~/state/device",
            "{{ value_json.uptimeSeconds }}",
            device_class="duration",
            unit="s",
        ),
        "ram": _sensor(
            uid,
            prefix,
            "ram",
            "Free RAM",
            "~/state/device",
            "{{ value_json.freeHeapBytes }}",
            "mdi:memory",
            device_class="data_size",
            unit="B",
        ),
        "btnl": _binary_button(uid, prefix, "btnl", "left", "Button left"),
        "btnm": _binary_button(uid, prefix, "btnm", "select", "Button select"),
        "btnr": _binary_button(uid, prefix, "btnr", "right", "Button right"),
    }
    if engine.adapter.get("batteryPercent") is not None:
        components["bat"] = _sensor(
            uid,
            prefix,
            "bat",
            "Battery",
            "~/state/device",
            "{{ value_json.batteryPercent }}",
            device_class="battery",
            unit="%",
        )
        components["lowbat"] = {
            "~": prefix,
            "p": "binary_sensor",
            "name": "Low battery",
            "dev_cla": "battery",
            "stat_t": "~/state/device",
            "val_tpl": "{{ 'ON' if value_json.batteryPercent < 15 else 'OFF' }}",
            "uniq_id": f"{uid}_lowbat",
        }
    if engine.config.compatibility.extensions:
        components["tc002_volume"] = {
            "~": prefix,
            "p": "number",
            "name": "TC002 volume",
            "uniq_id": f"{uid}_tc002_volume",
            "cmd_t": "~/extensions/audio/volume/set",
            "stat_t": "~/state/audio",
            "val_tpl": "{{ value_json.volume }}",
            "min": 0,
            "max": 100,
            "step": 1,
        }
        components["tc002_knob"] = {
            "~": prefix,
            "p": "event",
            "name": "TC002 knob",
            "uniq_id": f"{uid}_tc002_knob",
            "stat_t": "~/extensions/events/knob",
            "event_types": ["clockwise", "anticlockwise", "press", "release"],
        }
    return {
        "avty_t": f"{prefix}/availability",
        "pl_avail": "online",
        "pl_not_avail": "offline",
        "dev": {
            "ids": uid,
            "name": engine.config.name,
            "sw": __version__,
            "mf": "Ulanzi / community bridge",
            "mdl": "TC002 Pixbar",
        },
        "o": {"name": "tc002-awtrix-bridge", "sw": __version__},
        "cmps": components,
    }


def _indicator(uid: str, prefix: str, number: int) -> dict[str, Any]:
    index = number - 1
    return {
        "~": prefix,
        "p": "light",
        "name": f"Indicator {number}",
        "ic": [
            "mdi:arrow-top-right-thick",
            "mdi:arrow-right-thick",
            "mdi:arrow-bottom-right-thick",
        ][index],
        "on_cmd_type": "first",
        "cmd_t": f"~/cmd/indicators/{number}",
        "pl_on": '{"color":[255,255,255]}',
        "pl_off": '{"color":[0,0,0]}',
        "stat_t": "~/state/device",
        "stat_val_tpl": (
            f'{{% if value_json.indicators[{index}].on %}}{{"color":[255,255,255]}}'
            '{% else %}{"color":[0,0,0]}{% endif %}'
        ),
        "rgb_cmd_t": f"~/cmd/indicators/{number}",
        "rgb_cmd_tpl": '{"color":[{{ red }},{{ green }},{{ blue }}]}',
        "rgb_stat_t": "~/state/device",
        "rgb_val_tpl": (
            f"{{{{ value_json.indicators[{index}].color[1:3]|int(base=16) }}}},"
            f"{{{{ value_json.indicators[{index}].color[3:5]|int(base=16) }}}},"
            f"{{{{ value_json.indicators[{index}].color[5:7]|int(base=16) }}}}"
        ),
        "uniq_id": f"{uid}_ind{number}",
    }


def _button(uid: str, prefix: str, key: str, name: str, suffix: str, icon: str) -> dict[str, Any]:
    return {
        "~": prefix,
        "p": "button",
        "name": name,
        "ic": icon,
        "cmd_t": f"~/cmd/{suffix}",
        "pl_prs": "{}",
        "uniq_id": f"{uid}_{key}",
    }


def _sensor(
    uid: str,
    prefix: str,
    key: str,
    name: str,
    topic: str,
    template: str | None,
    icon: str | None = None,
    *,
    device_class: str | None = None,
    unit: str | None = None,
) -> dict[str, Any]:
    value: dict[str, Any] = {
        "~": prefix,
        "p": "sensor",
        "name": name,
        "stat_t": topic,
        "uniq_id": f"{uid}_{key}",
    }
    if template:
        value["val_tpl"] = template
    if icon:
        value["ic"] = icon
    if device_class:
        value["dev_cla"] = device_class
    if unit:
        value["unit_of_meas"] = unit
    return value


def _binary_button(uid: str, prefix: str, key: str, button: str, name: str) -> dict[str, Any]:
    return {
        "~": prefix,
        "p": "binary_sensor",
        "name": name,
        "ic": "mdi:gesture-tap-button",
        "stat_t": f"~/state/buttons/{button}",
        "pl_on": "1",
        "pl_off": "0",
        "uniq_id": f"{uid}_{key}",
    }
