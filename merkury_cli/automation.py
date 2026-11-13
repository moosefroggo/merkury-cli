"""Payloads and queries for Pepper groups, scenes, schedules, and timers."""

import argparse
import json
import re
from datetime import datetime, timedelta, timezone

from requests import HTTPError

from .lighting import address, group_id, identifiers, select_bulb, select_named, settings
from .pepper import device_name, pepper_device_id

COLLECTIONS = {
    "group": ("/account/deviceGroups", "groups", "name"),
    "scene": ("/account/scenes", "scenes", "name"),
    "rule": ("/account/rules", "rules", "rule_name"),
}
DAYS = ("Sun", "Mon", "Tue", "Wed", "Thu", "Fri", "Sat")


def group_path(client, group):
    account = group.get("account_id") or group.get("accountId") or client.account_id
    return f"/account/deviceGroups/accountId/{address(account)}/groupId/{address(group_id(group))}"


def group_payload(client, name, members, state):
    bulbs = client.bulbs()
    devices = {}
    for member in members:
        bulb = select_bulb(bulbs, member, state)
        device_id = pepper_device_id(bulb)
        devices[device_id] = bulb.get("pepperDevice_ObjectId") or device_id
    return {"name": name, "type": "bulb_group", "devices": list(devices.values())}


def scene_actions(bulbs, power, brightness, color, white, preset):
    parameters = {"brightness": brightness, "color": color, "white": white, "preset": preset}
    values = settings(power) if power else {}
    for name, value in parameters.items():
        if value is not None:
            values.update(settings(name, value))
    if not values:
        raise ValueError("Specify --power, --brightness, --color, --white, or --preset.")
    return [
        {"device_id": pepper_device_id(bulb), "action_type": "setting", "target_state": json.dumps({key: value})}
        for bulb in bulbs for key, value in values.items()
    ]


def time_offset(value):
    if not re.fullmatch(r"(?:[01]\d|2[0-3]):[0-5]\d", value):
        raise argparse.ArgumentTypeError("Use a 24-hour time such as 18:30.")
    hour, minute = map(int, value.split(":"))
    return (hour * 60 + minute) * 60_000


def week_days(value):
    if value.casefold() in ("daily", "all"):
        return list(DAYS)
    choices = {day.casefold(): day for day in DAYS}
    tokens = [day.strip().casefold() for day in value.split(",")]
    if not all(token in choices for token in tokens):
        raise argparse.ArgumentTypeError("Use daily or comma-separated days such as Mon,Wed,Fri.")
    return [day for day in DAYS if day.casefold() in tokens]


def schedule_actions(bulbs, power, brightness, color, white, preset):
    commands = {
        key: value for key, value in {
            "bulbBrightness": brightness, "bulbColor": color, "bulbColorTemp": white, "ledPattern": preset,
        }.items() if value is not None
    }
    return {
        "actions": [{"device_id": pepper_device_id(bulb), "commands": [f"power_{power}"]} for bulb in bulbs],
        "settings": [{"device_id": pepper_device_id(bulb), "commands": commands} for bulb in bulbs],
    }


def scheduled_scene(client, name):
    path, collection, name_key = COLLECTIONS["scene"]
    scene = select_named(client.collection(path, collection), name, name_key, ("scene_id", "id", "_id"))
    return {"actions": [{"scene_id": scene.get("scene_id") or scene["id"], "commands": ["scene"]}], "settings": []}


def rule_update(record):
    fields = ("id", "actions", "device_id", "detectionType", "detectedFaceId", "enabled", "event_type",
              "provider", "rule_name", "settings", "smart_detection_types", "timeFrames", "linkedRule")
    return {key: record[key] for key in fields if key in record}


def duration(value):
    match = re.fullmatch(r"(\d+)([mh]?)", value.casefold())
    if not match:
        raise argparse.ArgumentTypeError("Use a whole number of minutes (30m) or hours (2h).")
    minutes = int(match[1]) * (60 if match[2] == "h" else 1)
    if not 1 <= minutes <= 1440:
        raise argparse.ArgumentTypeError("Timer length must be between 1 minute and 24 hours.")
    return minutes


def timers(bulb):
    value = bulb.get("timers") or []
    return value if isinstance(value, list) else []


def active_timers(bulb, state):
    current = timers(bulb)
    known = state.get("timers", {}).get(pepper_device_id(bulb))
    if known and known["ends_at"] > datetime.now(timezone.utc).timestamp():
        if current:
            return [{**item, **known} if item.get("id") == known["id"] else item for item in current]
        return [known]
    return current


def timer_path(bulb):
    return f"/account/devices/{address(pepper_device_id(bulb))}/timers"


def set_timer(client, bulb, power, minutes, state):
    if active_timers(bulb, state):
        raise ValueError("This bulb already has a known timer. Cancel it before starting another.")
    now = datetime.now(timezone.utc)
    created = now.isoformat(timespec="milliseconds").replace("+00:00", "Z")
    action = {
        "timerId": "timer", "duration": minutes, "createTime": created,
        "command": {"name": "light.stateOn", "value": int(power == "on")},
    }
    response = client.request("POST", timer_path(bulb), {"timers": [action]})
    if not isinstance(response, dict) or not response.get("id"):
        raise ValueError("Pepper did not confirm timer creation. Run timer cancel before retrying.")
    end = now + timedelta(minutes=minutes)
    return {"id": "timer", "power": power, "ends_at": end.timestamp(),
            "end_time": end.astimezone().isoformat(timespec="seconds"),
            "source": "CLI receipt"}


def cancel_timer(client, bulb, timer_id):
    try:
        client.request("DELETE", f"{timer_path(bulb)}/{address(timer_id)}")
    except HTTPError as error:
        if error.response.status_code != 404:
            raise


def activity(client, bulbs, days):
    now = datetime.now(timezone.utc)
    end = now.strftime("%Y%m%dT%H%M%SZ")
    start = (now - timedelta(days=days)).strftime("%Y%m%dT%H%M%SZ")
    by_id = {identifier: bulb for bulb in bulbs for identifier in identifiers(bulb) if identifier}
    entries = client.collection(f"/account/activity/startTime/{start}/endTime/{end}", "activity")
    result = []
    for event in entries:
        event_id = event.get("deviceId") or event.get("device_id")
        if event_id in by_id:
            settings = event.get("settings") or {}
            result.append({
                "device": device_name(by_id[event_id]), "time": event.get("created"),
                "event": event.get("type"), "state": settings.get("light.stateOn"),
            })
    return result


def normalize_scene_actions(actions):
    return [
        {
            "device_id": action.get("entity_id") or action.get("device_id"),
            "target_state": action.get("executor_property") or action.get("target_state"),
            "action_type": action.get("action_executor") or action.get("action_type"),
        }
        for action in actions
    ]
