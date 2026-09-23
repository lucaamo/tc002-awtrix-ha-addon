from __future__ import annotations

import base64
import binascii
import colorsys
import re
from copy import deepcopy
from typing import Any

from .errors import validation

APP_NAME_RE = re.compile(r"^[A-Za-z0-9_-]{1,32}$")

DISPLAY_TEMPLATES = ("inherit", "focus", "labelled")

APP_KEYS = {
    "displayTemplate",
    "displayTitle",
    "text",
    "textCase",
    "font",
    "textInFront",
    "textCenter",
    "textColor",
    "textBlinkMs",
    "textFadeMs",
    "textOffsetX",
    "backgroundColor",
    "icon",
    "iconMode",
    "iconOffsetX",
    "durationMs",
    "scroll",
    "repeat",
    "lifetimeMs",
    "lifetimeExpiry",
    "palette",
    "paletteBlend",
    "paletteSpan",
    "paletteSpeed",
    "barChart",
    "lineChart",
    "chartAutoscale",
    "chartColor",
    "progress",
    "progressColor",
    "progressTrackColor",
    "effect",
    "effectSpeed",
    "overlay",
    "draw",
}
NOTIFICATION_KEYS = APP_KEYS | {
    "name",
    "hold",
    "stack",
    "wakeup",
    "sound",
    "soundRtttl",
    "soundLoop",
}

SCROLL_KEYS = {"mode", "direction", "entry", "whenFits", "speed", "gap", "holdMs"}
SCROLL_ENUMS = {
    "mode": {"static", "wrap", "loop", "bounce"},
    "direction": {"left", "right"},
    "entry": {"inline", "offscreen"},
    "whenFits": {"static", "scroll"},
}

TRANSITIONS = [
    "Random",
    "Slide",
    "Dim",
    "Zoom",
    "Rotate",
    "Pixelate",
    "Curtain",
    "Ripple",
    "Blink",
    "Reload",
    "Fade",
    "Cover",
    "Uncover",
    "Split",
    "Blinds",
    "Blocks",
    "Flash",
    "Diamond",
    "Wave",
    "Rain",
    "Melt",
    "Interlace",
]
PALETTES = {"Cloud", "Lava", "Ocean", "Forest", "Stripe", "Party", "Heat", "Rainbow"}
FONT_FAMILIES = (
    "silkscreen",
    "scientifica",
    "scientifica_italic",
    "awtrix_1x",
    "awtrix",
)
DISPLAY_LAYOUTS = ("singleLine", "twoLine")

DEFAULT_SETTINGS: dict[str, Any] = {
    "autoBrightness": False,
    "brightness": 120,
    "autoTransition": True,
    "textColor": "#FFFFFF",
    "transitionEffect": "Rain",
    "transitionDurationMs": 1000,
    "appDurationMs": 7000,
    "musicTitleScrollRepeats": 3,
    "timeMode": 1,
    "calendarHeaderColor": "#FF0000",
    "calendarTextColor": "#000000",
    "calendarBodyColor": "#FFFFFF",
    "time24h": True,
    "timeLeadingZero": True,
    "timeShowSeconds": False,
    "timeShowAmPm": False,
    "timeSeparatorMode": "pulse",
    "dateOrder": "dayMonthYear",
    "dateSeparator": "dot",
    "dateYearMode": "twoDigit",
    "dateShowWeekday": False,
    "dateMonthNames": False,
    "useCelsius": True,
    "blockNavigation": False,
    "soundEnabled": True,
    "knobSoundEnabled": True,
    "uppercase": True,
    "fontFamily": "scientifica",
    "timeDateFontSize": "small",
    "displayLayout": "singleLine",
    "appTitles": {},
    "timeColor": None,
    "dateColor": None,
    "humidityColor": None,
    "temperatureColor": None,
    "batteryColor": None,
    "buzzerVolume": 80,
    "dfplayerVolume": 80,
    "mp3Volume": 70,
    "radioVolume": 60,
    "radioMeta": True,
    "saturation": 100,
    "gamma": 1.9,
    "colorCorrection": None,
    "colorTint": None,
    "scroll": {
        "mode": "wrap",
        "direction": "left",
        "entry": "inline",
        "whenFits": "static",
        "speed": 100,
        "gap": 8,
        "holdMs": 1000,
    },
    "weekdayBar": {
        "show": True,
        "startOnMonday": True,
        "weekendDays": ["sunday", "saturday"],
        "activeColor": "#FFFFFF",
        "inactiveColor": "#666666",
        "weekendActiveColor": "#FFFFFF",
        "weekendInactiveColor": "#666666",
    },
}

BOOLEAN_SETTINGS = {
    "autoBrightness",
    "autoTransition",
    "time24h",
    "timeLeadingZero",
    "timeShowSeconds",
    "timeShowAmPm",
    "dateShowWeekday",
    "dateMonthNames",
    "useCelsius",
    "blockNavigation",
    "soundEnabled",
    "knobSoundEnabled",
    "uppercase",
    "radioMeta",
}
RANGED_SETTINGS = {
    "brightness": (0, 255),
    "timeMode": (0, 6),
    "buzzerVolume": (0, 100),
    "dfplayerVolume": (0, 100),
    "mp3Volume": (0, 100),
    "radioVolume": (0, 100),
    "saturation": (0, 100),
    "transitionDurationMs": (0, 2_147_483_647),
    "appDurationMs": (0, 2_147_483_647),
    "musicTitleScrollRepeats": (1, 10),
}
COLOR_SETTINGS = {
    "textColor",
    "calendarHeaderColor",
    "calendarTextColor",
    "calendarBodyColor",
    "timeColor",
    "dateColor",
    "humidityColor",
    "temperatureColor",
    "batteryColor",
    "colorCorrection",
    "colorTint",
}


def validate_app_name(name: str) -> str:
    if not isinstance(name, str) or not APP_NAME_RE.fullmatch(name):
        raise validation("App name must match [A-Za-z0-9_-]{1,32}", "name")
    return name


def parse_color(value: Any, field: str = "color", *, nullable: bool = False) -> int | None:
    if value is None and nullable:
        return None
    if isinstance(value, bool):
        raise validation("Color must not be boolean", field)
    if isinstance(value, int) and 0 <= value <= 0xFFFFFF:
        return value
    if isinstance(value, str):
        text = value.removeprefix("#")
        if len(text) == 3 and all(char in "0123456789abcdefABCDEF" for char in text):
            text = "".join(char * 2 for char in text)
        if len(text) == 6 and all(char in "0123456789abcdefABCDEF" for char in text):
            return int(text, 16)
    if isinstance(value, list):
        if (
            len(value) == 3
            and all(isinstance(v, int) and not isinstance(v, bool) for v in value)
            and all(0 <= v <= 255 for v in value)
        ):
            return (value[0] << 16) | (value[1] << 8) | value[2]
        if (
            len(value) == 4
            and isinstance(value[0], str)
            and value[0].upper() == "HSV"
            and all(isinstance(v, (int, float)) and not isinstance(v, bool) for v in value[1:])
        ):
            h, s, v = float(value[1]), float(value[2]), float(value[3])
            if 0 <= h <= 360 and 0 <= s <= 100 and 0 <= v <= 100:
                red, green, blue = colorsys.hsv_to_rgb(h / 360.0, s / 100.0, v / 100.0)
                return (round(red * 255) << 16) | (round(green * 255) << 8) | round(blue * 255)
    raise validation("Color must be #RGB, #RRGGBB, 0xRRGGBB, [r,g,b], or [HSV,h,s,v]", field)


def _integer(value: Any, field: str, minimum: int = 0, maximum: int = 2_147_483_647) -> int:
    if not isinstance(value, int) or isinstance(value, bool) or not minimum <= value <= maximum:
        raise validation(f"Must be an integer between {minimum} and {maximum}", field)
    return value


def _validate_scroll(
    value: Any, field: str = "scroll", *, include_defaults: bool = True
) -> dict[str, Any]:
    if not isinstance(value, dict):
        raise validation("Scroll must be an object", field)
    unknown = set(value) - SCROLL_KEYS
    if unknown:
        key = min(unknown)
        raise validation(f"Unknown scroll field: {key}", f"{field}.{key}")
    result = deepcopy(DEFAULT_SETTINGS["scroll"]) if include_defaults else {}
    for key, item in value.items():
        target = f"{field}.{key}"
        if key in SCROLL_ENUMS:
            if not isinstance(item, str) or item not in SCROLL_ENUMS[key]:
                raise validation(f"Invalid {key}", target)
        else:
            item = _integer(item, target)
        result[key] = item
    return result


def validate_app_payload(
    payload: Any, *, notification: bool = False, strict: bool = True
) -> dict[str, Any]:
    if not isinstance(payload, dict):
        raise validation("Payload must be an object")
    allowed = NOTIFICATION_KEYS if notification else APP_KEYS
    unknown = set(payload) - allowed
    if unknown and strict:
        key = min(unknown)
        raise validation(f"Unknown field: {key}", key)
    result = {key: deepcopy(value) for key, value in payload.items() if key in allowed}

    for key in {"backgroundColor", "progressTrackColor"} & result.keys():
        parse_color(result[key], key)
    for key in {"textColor", "chartColor", "progressColor"} & result.keys():
        if not (isinstance(result[key], str) and result[key].lower() == "palette"):
            parse_color(result[key], key)
    if "palette" in result:
        palette = result["palette"]
        if isinstance(palette, str):
            match = next((name for name in PALETTES if name.lower() == palette.lower()), None)
            if match is None:
                raise validation("Unknown palette", "palette")
            result["palette"] = match
        elif isinstance(palette, list) and palette:
            for index, color in enumerate(palette):
                parse_color(color, f"palette.{index}")
        else:
            raise validation("palette must be a known name or non-empty color array", "palette")
    if "displayTemplate" in result and result["displayTemplate"] not in DISPLAY_TEMPLATES:
        raise validation("Unknown display template", "displayTemplate")
    if "displayTitle" in result:
        title = result["displayTitle"]
        if not isinstance(title, str) or len(title) > 32 or any(ord(c) < 32 for c in title):
            raise validation("Use a single-line title of up to 32 characters", "displayTitle")
    if "text" in result:
        text = result["text"]
        if isinstance(text, list):
            for index, fragment in enumerate(text):
                if not isinstance(fragment, dict) or set(fragment) - {"text", "color"}:
                    raise validation(
                        "Text fragment must contain text and optional color", f"text.{index}"
                    )
                if not isinstance(fragment.get("text", ""), str):
                    raise validation("Fragment text must be a string", f"text.{index}.text")
                if "color" in fragment:
                    parse_color(fragment["color"], f"text.{index}.color")
        elif not isinstance(text, str):
            # Upstream accepts the known key but leaves the default empty string.
            result.pop("text")
    for key in {"textInFront", "textCenter", "chartAutoscale", "paletteBlend"} & result.keys():
        if not isinstance(result[key], bool):
            if key == "chartAutoscale":
                result[key] = True
            else:
                result.pop(key)
    for key in {"durationMs", "repeat", "lifetimeMs"} & result.keys():
        if not isinstance(result[key], int) or isinstance(result[key], bool):
            result[key] = 0
    for key in {"textBlinkMs", "textFadeMs", "paletteSpan"} & result.keys():
        if not isinstance(result[key], int) or isinstance(result[key], bool):
            result.pop(key)
    if "paletteSpan" in result:
        result["paletteSpan"] = max(0, min(0xFFFF, result["paletteSpan"]))
    for key in {"effectSpeed", "paletteSpeed"} & result.keys():
        if not isinstance(result[key], (int, float)) or isinstance(result[key], bool):
            result.pop(key)
    for key in {"textOffsetX", "iconOffsetX"} & result.keys():
        if not isinstance(result[key], int) or isinstance(result[key], bool):
            result.pop(key)
    if "icon" in result and not isinstance(result["icon"], str):
        result.pop("icon")
    for key in {"effect", "overlay"} & result.keys():
        if not isinstance(result[key], str):
            result.pop(key)
    if "font" in result and result["font"] not in {"small", "large"}:
        raise validation("Font must be small or large", "font")
    if "textCase" in result and result["textCase"] not in {"inherit", "upper", "asTyped"}:
        raise validation("Invalid textCase", "textCase")
    if "iconMode" in result and result["iconMode"] not in {"fixed", "pushOnce", "push"}:
        raise validation("Invalid iconMode", "iconMode")
    if "lifetimeExpiry" in result and result["lifetimeExpiry"] not in {"remove", "mark"}:
        raise validation("Invalid lifetimeExpiry", "lifetimeExpiry")
    if "scroll" in result:
        # App-level scrolling is a partial override. Keeping only explicitly
        # supplied fields lets the global dashboard speed and hold values apply.
        result["scroll"] = _validate_scroll(result["scroll"], include_defaults=False)
    for key in {"barChart", "lineChart"} & result.keys():
        if not isinstance(result[key], list):
            result[key] = []
        else:
            result[key] = [
                int(item) if isinstance(item, (int, float)) and not isinstance(item, bool) else 0
                for item in result[key][:16]
            ]
    if "progress" in result:
        value = result["progress"]
        result["progress"] = (
            int(value) if isinstance(value, (int, float)) and not isinstance(value, bool) else -1
        )
    if "draw" in result:
        _validate_draw(result["draw"])
    if notification:
        for key in {"hold", "stack", "wakeup", "soundLoop"} & result.keys():
            if not isinstance(result[key], bool):
                result.pop(key)
        if "name" in result and not isinstance(result["name"], str):
            result.pop("name")
        if "sound" in result:
            if isinstance(result["sound"], int) and not isinstance(result["sound"], bool):
                result["sound"] = str(result["sound"])
            elif not isinstance(result["sound"], str):
                result.pop("sound")
        if "soundRtttl" in result and not isinstance(result["soundRtttl"], str):
            result.pop("soundRtttl")
    return result


def _validate_draw(commands: Any) -> None:
    if not isinstance(commands, list):
        raise validation("draw must be an array", "draw")
    shapes = {
        "pixel": (2, True),
        "line": (4, True),
        "rect": (4, True),
        "rectFill": (4, True),
        "circle": (3, True),
        "circleFill": (3, True),
        "text": (3, True),
        "bitmap": (5, False),
    }
    for index, command in enumerate(commands):
        field = f"draw[{index}]"
        if not isinstance(command, list) or not command or not isinstance(command[0], str):
            raise validation("Each draw command must be an array with a name first", field)
        name = command[0]
        if name == "pixels":
            if len(command) < 4 or (len(command) - 2) % 2:
                raise validation("pixels requires color and x,y pairs", field)
            if command[1] is not None:
                parse_color(command[1], field)
            if not all(
                isinstance(value, int) and not isinstance(value, bool) for value in command[2:]
            ):
                raise validation("pixels coordinates must be integers", field)
            continue
        if name not in shapes:
            raise validation(f"Unknown draw command: {name}", field)
        argument_count, takes_color = shapes[name]
        expected = argument_count + 1
        if len(command) not in ({expected, expected + 1} if takes_color else {expected}):
            raise validation(f"{name} has the wrong number of arguments", field)
        numeric = 2 if name in {"pixel", "text"} else argument_count
        if name == "bitmap":
            numeric = 4
        if not all(
            isinstance(value, int) and not isinstance(value, bool)
            for value in command[1 : 1 + numeric]
        ):
            raise validation(f"{name} coordinates must be integers", field)
        if name == "text" and not isinstance(command[3], str):
            raise validation("text draw command requires a string", field)
        if takes_color and len(command) == expected + 1:
            parse_color(command[-1], field)
        if name == "bitmap":
            data = command[5]
            if isinstance(data, str):
                try:
                    base64.b64decode(data, validate=True)
                except (ValueError, binascii.Error) as error:
                    raise validation("bitmap data is not valid base64", field) from error
            elif isinstance(data, list):
                for color in data:
                    parse_color(color, field)
            else:
                raise validation("bitmap data must be base64 or a color array", field)


def validate_pushed_payload(payload: Any, *, strict: bool = True) -> list[dict[str, Any]]:
    items = payload if isinstance(payload, list) else [payload]
    if not items:
        raise validation("Pushed app array must not be empty")
    return [validate_app_payload(item, strict=strict) for item in items]


def validate_notification_payload(payload: Any, *, strict: bool = True) -> dict[str, Any]:
    if isinstance(payload, list):
        if len(payload) != 1:
            raise validation("Notification payload must contain exactly one item")
        payload = payload[0]
    return validate_app_payload(payload, notification=True, strict=strict)


def validate_settings_patch(patch: Any) -> dict[str, Any]:
    if not isinstance(patch, dict):
        raise validation("Settings patch must be an object")
    unknown = set(patch) - set(DEFAULT_SETTINGS)
    if unknown:
        key = min(unknown)
        raise validation(f"Unknown settings field: {key}", key)
    result = deepcopy(patch)
    for key, value in patch.items():
        if key in BOOLEAN_SETTINGS and not isinstance(value, bool):
            raise validation("Must be boolean", key)
        if key in RANGED_SETTINGS:
            minimum, maximum = RANGED_SETTINGS[key]
            _integer(value, key, minimum, maximum)
        if key in COLOR_SETTINGS:
            result[key] = None if value is None else f"#{parse_color(value, key):06X}"
        if key == "gamma" and (
            not isinstance(value, (int, float)) or isinstance(value, bool) or value <= 0
        ):
            raise validation("Gamma must be greater than zero", key)
        if key == "transitionEffect":
            match = next((item for item in TRANSITIONS if item.lower() == str(value).lower()), None)
            if match is None:
                raise validation("Unknown transition effect", key)
            result[key] = match
        if key == "fontFamily" and value not in FONT_FAMILIES:
            raise validation("Invalid TC002 font family", key)
        if key == "timeDateFontSize" and value not in {"small", "large"}:
            raise validation("Invalid Time and Date font size", key)
        if key == "displayLayout" and value not in DISPLAY_LAYOUTS:
            raise validation("Invalid TC002 display layout", key)
        if key == "appTitles":
            if not isinstance(value, dict) or len(value) > 128:
                raise validation("App titles must be an object with up to 128 entries", key)
            titles = {}
            for name, title in value.items():
                validate_app_name(name)
                if (
                    not isinstance(title, str)
                    or len(title) > 64
                    or any(ord(character) < 32 or ord(character) == 127 for character in title)
                ):
                    raise validation("Use a single-line title of up to 64 characters", f"appTitles.{name}")
                if title.strip():
                    titles[name] = title.strip()
            result[key] = titles
        if key == "timeSeparatorMode" and value not in {"steady", "blink", "pulse"}:
            raise validation("Invalid time separator mode", key)
        if key == "dateOrder" and value not in {"dayMonthYear", "monthDayYear", "yearMonthDay"}:
            raise validation("Invalid date order", key)
        if key == "dateSeparator" and value not in {"dot", "slash", "dash"}:
            raise validation("Invalid date separator", key)
        if key == "dateYearMode" and value not in {"none", "twoDigit", "fourDigit"}:
            raise validation("Invalid date year mode", key)
        if key == "scroll":
            result[key] = _validate_scroll(value)
        if key == "weekdayBar":
            if value is None:
                result[key] = {}
                continue
            if not isinstance(value, dict):
                raise validation("weekdayBar must be an object", key)
            allowed = {
                "show",
                "startOnMonday",
                "weekendDays",
                "activeColor",
                "inactiveColor",
                "weekendActiveColor",
                "weekendInactiveColor",
            }
            unknown_weekday = set(value) - allowed
            if unknown_weekday:
                nested = f"weekdayBar.{min(unknown_weekday)}"
                raise validation("unknown field", nested)
            normalized = deepcopy(value)
            for boolean_key in {"show", "startOnMonday"} & value.keys():
                if not isinstance(value[boolean_key], bool):
                    raise validation("must be a boolean", f"weekdayBar.{boolean_key}")
            if "weekendDays" in value:
                days = value["weekendDays"]
                valid_days = {
                    "sunday",
                    "monday",
                    "tuesday",
                    "wednesday",
                    "thursday",
                    "friday",
                    "saturday",
                }
                if not isinstance(days, list) or any(day not in valid_days for day in days):
                    raise validation(
                        "must be an array of weekday names", "weekdayBar.weekendDays"
                    )
                normalized["weekendDays"] = [
                    day
                    for day in (
                        "sunday",
                        "monday",
                        "tuesday",
                        "wednesday",
                        "thursday",
                        "friday",
                        "saturday",
                    )
                    if day in days
                ]
            for color_key in {
                "activeColor",
                "inactiveColor",
                "weekendActiveColor",
                "weekendInactiveColor",
            } & value.keys():
                normalized[color_key] = f"#{parse_color(value[color_key], f'weekdayBar.{color_key}'):06X}"
            result[key] = normalized
    return result
