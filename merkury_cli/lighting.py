"""Bulb selection, lighting controls, and recent colors."""

import argparse
import json
import os
import re
import tempfile
from pathlib import Path
from time import monotonic, sleep
from urllib.parse import quote

import requests

from .pepper import device_name, pepper_device_id

PRESETS = ("night", "reading", "cocktail", "leisure", "soft", "nature", "blinking", "rave", "rainbow")
COLORS = {
    "red": "FF0000", "green": "00FF00", "blue": "0000FF", "amber": "FFBF00",
    "orange": "FF6600", "purple": "8000FF", "pink": "FF69B4", "white": "FFFFFF",
    "cyan": "00FFFF", "yellow": "FFFF00",
}
STATE_PATH = Path.home() / "Library" / "Application Support" / "Merkury CLI" / "state.json"


def color_code(value):
    value = COLORS.get(value.casefold(), value.upper().removeprefix("#").removeprefix("0X"))
    if not re.fullmatch(r"[0-9A-F]{6}", value):
        raise argparse.ArgumentTypeError("Use a color name such as amber or a hex color such as FF6600.")
    return value


def bounded_int(low, high):
    def parse(value):
        try:
            number = int(value)
        except ValueError:
            number = None
        if number is None or not low <= number <= high:
            raise argparse.ArgumentTypeError(f"Enter a whole number from {low} to {high}.")
        return number
    return parse


def load_state(account):
    try:
        data = json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        return {}
    except (ValueError, OSError):
        raise ValueError("Could not read the Merkury CLI settings file.") from None
    return data.get(account, {})


def save_state(account, state):
    STATE_PATH.parent.mkdir(parents=True, exist_ok=True, mode=0o700)
    try:
        all_accounts = json.loads(STATE_PATH.read_text())
    except FileNotFoundError:
        all_accounts = {}
    except ValueError:
        raise ValueError("Could not read the Merkury CLI settings file.") from None
    all_accounts[account] = state
    with tempfile.NamedTemporaryFile(mode="w", dir=STATE_PATH.parent, delete=False) as file:
        temporary = Path(file.name)
        try:
            json.dump(all_accounts, file, indent=2)
            file.close()
            os.replace(temporary, STATE_PATH)
        finally:
            temporary.unlink(missing_ok=True)


def group_id(group):
    return group.get("_id") or group["id"]


def select_named(items, selector, name_key="name", id_keys=("id", "_id")):
    exact = [
        item for item in items
        if selector in [item.get(key) for key in id_keys]
        or str(item.get(name_key, "")).casefold() == selector.casefold()
    ]
    if len(exact) == 1:
        return exact[0]
    if not exact:
        raise ValueError(f"No match for {selector!r}. Check the list command.")
    raise ValueError(f"More than one match for {selector!r}. Use the ID from the list command.")


def select_bulb(bulbs, selector, state):
    alias = state.get("aliases", {}).get(selector.casefold())
    matches = [
        bulb for bulb in bulbs
        if selector in identifiers(bulb)
        or selector.casefold() == device_name(bulb).casefold()
        or alias == pepper_device_id(bulb)
    ]
    if len(matches) != 1:
        reason = "No bulb matches" if not matches else "Multiple bulbs match"
        raise ValueError(f"{reason} {selector!r}. Use an exact name, alias, or ID from merkury list.")
    return matches[0]


def identifiers(bulb):
    return {bulb.get(key) for key in ("id", "_id", "deviceId", "pepperDeviceId", "pepperDevice_ObjectId")}


def targets(client, args, state):
    selectors = getattr(args, "targets", [])
    if bool(selectors) + bool(args.all) + bool(args.group) != 1:
        raise ValueError("Specify bulb names, --all, or --group NAME.")
    bulbs = client.bulbs()
    if args.all:
        selected = bulbs
    elif args.group:
        group = select_named(client.collection("/account/deviceGroups", "groups"), args.group)
        members = set(group.get("devices") or group.get("deviceIds") or [])
        selected = [bulb for bulb in bulbs if identifiers(bulb) & members]
        if len(selected) != len(members):
            raise ValueError("Some members of this group could not be matched to bulbs.")
    else:
        by_id = {}
        for selector in selectors:
            bulb = select_bulb(bulbs, selector, state)
            by_id[pepper_device_id(bulb)] = bulb
        selected = list(by_id.values())
    if not selected:
        raise ValueError("There are no bulbs in that selection.")
    return selected


def summary(device):
    light = device.get("light") or {}
    reported_power = light.get("stateOn")
    power = {"1": "on", "0": "off", "true": "on", "false": "off"}.get(str(reported_power).lower(), "unknown")
    wifi = device.get("wifi") or {}
    firmware = device.get("firmware") or {}
    color = light.get("color")
    temperature = light.get("colorTemp")
    return {
        "name": device_name(device), "id": pepper_device_id(device), "model": device.get("model"),
        "power": power, "brightness": light.get("brightness"),
        "color": "#" + color.removeprefix("0x").removeprefix("#") if isinstance(color, str) and color else None,
        "temperature": temperature if temperature not in (None, 0, "0") else None,
        "mode": light.get("mode"), "preset": light.get("scene"),
        "account_status": device.get("status"), "wifi_status": wifi.get("status"),
        "firmware": firmware.get("version") if isinstance(firmware, dict) else firmware,
    }


def settings(command, value=None):
    if command in ("on", "off"):
        return {"light.stateOn": int(command == "on")}
    if command == "brightness":
        return {"light.brightness": value}
    if command == "color":
        return {"light.mode": "colour", "light.color": value}
    if command == "white":
        return {"light.mode": "white", "light.colorTemp": value}
    if command == "preset":
        return {"light.mode": "scene", "light.scene": value}
    raise ValueError(f"Unknown lighting action: {command}.")


def setting_matches(actual, desired):
    if isinstance(desired, str):
        if not isinstance(actual, str):
            return False
        return actual.casefold().removeprefix("#").removeprefix("0x") == desired.casefold()
    if isinstance(desired, int):
        return actual == desired or actual == str(desired)
    return actual == desired


def error_detail(error):
    response = error.response if isinstance(error, requests.RequestException) else None
    if response is not None:
        return f"Pepper returned HTTP {response.status_code}."
    return "The request timed out or the connection was interrupted."


def apply_settings(client, bulbs, build_settings, verify=True):
    results, pending = [], {}
    for bulb in bulbs:
        row = {"name": device_name(bulb), "result": "accepted", "settings_sent": []}
        results.append(row)
        values = build_settings(bulb)
        try:
            for setting, value in values.items():
                client.setting(bulb, setting, value)
                row["settings_sent"].append(setting)
        except requests.RequestException as error:
            row.update(result="partial" if row["settings_sent"] else "failed", error=error_detail(error))
            continue
        pending[pepper_device_id(bulb)] = (row, values)

    if verify and pending:
        deadline = monotonic() + 7
        while pending:
            try:
                current = {pepper_device_id(bulb): bulb.get("light") or {} for bulb in client.bulbs()}
            except requests.RequestException:
                break
            for device_id, (row, values) in list(pending.items()):
                observed = current.get(device_id, {})
                if all(setting_matches(observed.get(key.split(".", 1)[1]), value) for key, value in values.items()):
                    row["result"] = "confirmed"
                    del pending[device_id]
            if not pending or monotonic() >= deadline:
                break
            sleep(0.6)
        for row, _ in pending.values():
            row["result"] = "accepted, state unconfirmed"
    return results


def remember_color(state, bulbs, results, color):
    history = state.setdefault("colors", {})
    changed = False
    for bulb, row in zip(bulbs, results):
        if "light.color" not in row["settings_sent"]:
            continue
        device_id = pepper_device_id(bulb)
        recent = [existing for existing in history.get(device_id, []) if existing != color]
        history[device_id] = [color, *recent[:4]]
        changed = True
    return changed


def address(value):
    return quote(str(value), safe="")
