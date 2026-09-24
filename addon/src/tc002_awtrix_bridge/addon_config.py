from __future__ import annotations

import argparse
import json
from pathlib import Path

import yaml


def write_config(options_path: Path, output_path: Path) -> None:
    options = json.loads(options_path.read_text(encoding="utf-8"))
    config = {
        "uid": options["uid"],
        "name": options["name"],
        "timezone": options["timezone"],
        "compatibility": {
            "mode": "awtrix-ng",
            "strict": options["strict"],
            "extensions": options["extensions"],
        },
        "render": {"layout_mode": options["renderer_mode"], "fps": 20, "gamma": 1.9},
        "http": {"host": "0.0.0.0", "port": 7000, "username": "", "password": ""},
        "mqtt": {
            "enabled": True,
            "host": options["mqtt_host"],
            "port": options["mqtt_port"],
            "prefix": options["mqtt_prefix"],
            "client_id": f"tc002-awtrix-{options['uid']}",
            "keepalive": 30,
            "tls": False,
            "home_assistant_prefix": "homeassistant",
            "username": "",
            "password": "",
        },
        "adapter": {
            "enabled": True,
            "mode": options.get("adapter_mode", "stock_http"),
            "device_host": options["tc002_host"],
            "http_port": options.get("tc002_http_port", 80),
            "http_app": options.get("tc002_http_app", "awtrix_bridge"),
            "ng_mqtt_prefix": options.get("tc002_ng_mqtt_prefix", "awtrixNG"),
            "http_timeout": 3,
            "http_max_fps": options.get("tc002_http_max_fps", 10),
            "frame_port": 9876,
            "listen_host": "0.0.0.0",
            "event_port": 9877,
            "token": "",
            "heartbeat_timeout": 10,
            "blackout_timeout": options.get("tc002_http_frame_lifetime", 60),
            "allow_reboot": False,
            "allow_poweroff": False,
        },
        "persistence": {
            "directory": "/data",
            "persist_pushed_apps": False,
            "persist_current_app": False,
        },
    }
    temporary = output_path.with_suffix(".tmp")
    temporary.write_text(yaml.safe_dump(config, sort_keys=False), encoding="utf-8")
    temporary.replace(output_path)


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--options", type=Path, required=True)
    parser.add_argument("--output", type=Path, required=True)
    args = parser.parse_args()
    write_config(args.options, args.output)


if __name__ == "__main__":
    main()
