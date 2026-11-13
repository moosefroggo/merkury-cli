"""Control Merkury Smart bulbs from Terminal."""

import argparse
import json
import sys
from importlib.metadata import version

import argcomplete
import keyring
import requests
from playwright.sync_api import Error as BrowserError

from . import automation as auto
from . import lighting as lights
from . import pairing
from .pepper import (
    KEYCHAIN_ACCOUNT, KEYCHAIN_SERVICE, Pepper, device_name, pepper_device_id, sign_in, web_sign_in,
)


def command(subparsers, name, help_text, handler, **kwargs):
    parser = subparsers.add_parser(name, help=help_text, description=help_text, **kwargs)
    parser.add_argument("--json", action="store_true", default=argparse.SUPPRESS, help="Output JSON")
    parser.set_defaults(handler=handler)
    return parser


def selection(parser):
    parser.add_argument("targets", nargs="*", metavar="BULB", help="Bulb name, alias, or ID")
    parser.add_argument("--all", action="store_true", help="Target all paired bulbs")
    parser.add_argument("--group", help="Target an existing bulb group")


def mode_options(parser):
    options = parser.add_mutually_exclusive_group()
    options.add_argument("--color", type=lights.color_code)
    options.add_argument("--white", type=lights.bounded_int(2700, 6500), metavar="KELVIN")
    options.add_argument("--preset", choices=lights.PRESETS)
    parser.add_argument("--brightness", type=lights.bounded_int(1, 100), metavar="PERCENT")


def nonempty(value):
    value = value.strip()
    if not value:
        raise argparse.ArgumentTypeError("Enter a nonempty name.")
    return value


def destructive_flags(parser):
    parser.add_argument("--yes", action="store_true", help="Confirm the named operation")
    parser.add_argument("--dry-run", action="store_true", help="Preview without making changes")


def confirm(args, message):
    if args.yes:
        return
    if not sys.stdin.isatty() or args.json:
        raise ValueError(f"{message} Pass --yes to confirm outside an interactive terminal.")
    if input(f"{message} [y/N] ").strip().casefold() not in ("y", "yes"):
        raise ValueError("Cancelled.")


def display(args, data):
    if args.json:
        print(json.dumps(data, ensure_ascii=False, indent=2))
        return
    if isinstance(data, dict):
        data = [data]
    if not data:
        print("No results.")
    for row in data:
        if isinstance(row, dict):
            print("  |  ".join(f"{key.replace('_', ' ')}: {value}" for key, value in row.items() if value is not None))
        else:
            print(row)


def login(args, _client, _state):
    profile = sign_in() if args.email else web_sign_in()
    token = profile.get("lrToken")
    if not token:
        raise ValueError("Pepper did not return a reusable sign-in token.")
    client = Pepper(profile)
    count = len(client.bulbs())
    keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, token)
    display(args, {"result": "signed in", "bulbs": count})


def logout(args, _client, _state):
    if keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT):
        keyring.delete_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
    display(args, {"result": "signed out of this CLI"})


def bulb_list(args, client, state):
    if args.command in ("list", "devices") or not (args.targets or args.all or args.group):
        bulbs = client.bulbs()
    else:
        bulbs = lights.targets(client, args, state)
    rows = [lights.summary(bulb) for bulb in bulbs]
    if args.command in ("list", "devices") and not args.json:
        rows = [{key: row[key] for key in ("name", "power", "brightness", "wifi_status", "id")} for row in rows]
    display(args, rows)


def light_action(args, client, state):
    bulbs = lights.targets(client, args, state)
    action = getattr(args, "action", args.command)
    value = getattr(args, "value", None)
    if action == "reuse":
        bulb = bulbs[0]
        history = state.get("colors", {}).get(pepper_device_id(bulb), [])
        if len(history) < args.index:
            raise ValueError("No saved color at that position. See merkury history colors.")
        action, value = "color", history[args.index - 1]
    values = lights.settings(action, value)
    if args.dry_run:
        display(args, [{"name": device_name(bulb), "settings": values} for bulb in bulbs])
        return
    results = lights.apply_settings(client, bulbs, lambda _bulb: values, verify=not args.no_wait)
    if action == "color" and lights.remember_color(state, bulbs, results, value):
        lights.save_state(client.account_id, state)
    display(args, results)
    return int(any(row["result"] in ("failed", "partial") for row in results))


def history(args, client, state):
    bulbs = lights.targets(client, args, state)
    display(args, [
        {"name": device_name(bulb), "colors": state.get("colors", {}).get(pepper_device_id(bulb), [])}
        for bulb in bulbs
    ])


def alias(args, client, state):
    aliases = state.setdefault("aliases", {})
    if args.operation == "list":
        return display(args, [{"alias": name, "id": value} for name, value in sorted(aliases.items())])
    short = args.name.casefold()
    if args.operation == "remove":
        if short not in aliases:
            raise ValueError(f"No alias named {short!r}.")
        del aliases[short]
    else:
        bulbs = client.bulbs()
        bulb = lights.select_bulb(bulbs, args.bulb, state)
        if any(device_name(item).casefold() == short and item != bulb for item in bulbs):
            raise ValueError("That alias is already a different bulb's name.")
        aliases[short] = pepper_device_id(bulb)
    lights.save_state(client.account_id, state)
    display(args, {"alias": short, "result": args.operation})


def entity_row(kind, item):
    if kind == "group":
        return {"name": item.get("name"), "id": auto.group_id(item), "members": item.get("devices") or item.get("deviceIds") or []}
    if kind == "scene":
        return {"name": item.get("name"), "id": item.get("scene_id") or item.get("id") or item.get("_id"),
                "actions": item.get("actions") or []}
    frames = []
    for frame in item.get("timeFrames", []):
        minute = (frame.get("start_offset_ms") or 0) // 60_000
        frames.append(f"{minute // 60:02d}:{minute % 60:02d} {','.join(frame.get('days') or [])}".strip())
    return {"name": item.get("rule_name"), "id": item.get("id"), "enabled": item.get("enabled"),
            "trigger": item.get("event_type"), "schedule": frames}


def entities(args, client, state):
    kind, operation = args.command, args.operation
    path, collection, name_key = auto.COLLECTIONS[kind]
    records = client.collection(path, collection)
    if operation == "list":
        return display(args, [entity_row(kind, item) for item in records])
    item = None
    if operation != "add":
        item = lights.select_named(records, args.name, name_key, ("id", "_id", "scene_id"))
        row = entity_row(kind, item)
        item_id = lights.address(row["id"])
        if operation == "show":
            return display(args, item)
        if operation == "delete":
            if args.dry_run:
                return display(args, row)
            confirm(args, f"Delete {kind} {row['name']!r}?")
    name = getattr(args, "new_name", None) or args.name
    if kind == "group":
        if operation == "delete":
            client.request("DELETE", auto.group_path(client, item))
        else:
            if args.members:
                payload = auto.group_payload(client, name, args.members, state)
            elif item:
                payload = {"name": name, "type": item["type"], "devices": item.get("devices") or item.get("deviceIds")}
            else:
                raise ValueError("Supply the bulb names to add to the group.")
            client.request("PUT" if item else "POST", auto.group_path(client, item) if item else path, payload)
    elif kind == "scene":
        if operation in ("delete", "run"):
            client.request("DELETE" if operation == "delete" else "POST", f"{path}/{item_id}")
        else:
            payload = {
                "name": name,
                "location_id": getattr(args, "location", None) or (item or {}).get("location_id") or "0",
            }
            if item:
                payload["scene_id"] = row["id"]
                if item.get("background"):
                    payload["background"] = item["background"]
            if args.targets or args.all or args.group:
                bulbs = lights.targets(client, args, state)
                payload["actions"] = auto.scene_actions(bulbs, args.power, args.brightness, args.color, args.white, args.preset)
            elif item and not any((args.power, args.brightness, args.color, args.white, args.preset)):
                payload["actions"] = auto.normalize_scene_actions(item.get("actions") or [])
            else:
                raise ValueError("Specify bulbs and the scene settings.")
            client.request("PUT" if item else "POST", path, payload)
    else:
        if operation == "delete":
            client.request("DELETE", f"{path}/ruleId/{item_id}")
        elif operation in ("enable", "disable"):
            client.request("PUT", path, {**auto.rule_update(item), "enabled": operation == "enable"})
        else:
            payload = auto.rule_update(item) if item else {"version": 3, "event_type": "schedule", "smart_detection_types": []}
            payload["rule_name"] = name
            if item and item.get("event_type") != "schedule":
                raise ValueError("This editor supports scheduled rules. Use enable, disable, or delete for other rules.")
            if args.at is not None:
                old_frames = payload.get("timeFrames") or [{}]
                days = args.days or old_frames[0].get("days") or list(auto.DAYS)
                payload["timeFrames"] = [{"days": days, "start_offset_ms": args.at, "end_offset_ms": args.at}]
            elif args.days:
                if not payload.get("timeFrames"):
                    raise ValueError("Specify --at with --days.")
                payload["timeFrames"] = [{**frame, "days": args.days} for frame in payload["timeFrames"]]
            options = (args.brightness, args.color, args.white, args.preset)
            if args.scene:
                if args.targets or args.all or args.group or args.power or any(value is not None for value in options):
                    raise ValueError("Use --scene by itself when selecting a rule action.")
                payload.update(auto.scheduled_scene(client, args.scene))
            elif args.power is not None or any(value is not None for value in options):
                if not args.power:
                    raise ValueError("Specify --power when changing rule actions.")
                bulbs = lights.targets(client, args, state)
                payload.update(auto.schedule_actions(bulbs, args.power, *options))
            elif args.targets or args.all or args.group:
                raise ValueError("Specify --power when changing rule targets.")
            if not payload.get("timeFrames") or not payload.get("actions"):
                raise ValueError("Specifying --at, --power, and bulb targets is required to create a rule.")
            if args.dry_run:
                return display(args, payload)
            client.request("PUT" if item else "POST", path, payload)
    display(args, {"name": name, "operation": f"{kind} {operation}", "result": "accepted"})


def timer(args, client, state):
    if args.operation == "list" and not (args.targets or args.all or args.group):
        bulbs = client.bulbs()
    else:
        bulbs = lights.targets(client, args, state)
    if args.operation == "list":
        return display(args, [{"name": device_name(bulb), "timers": auto.active_timers(bulb, state)} for bulb in bulbs])
    saved_timers = state.setdefault("timers", {})
    results = []
    for bulb in bulbs:
        row = {"name": device_name(bulb), "result": "accepted"}
        device_id = pepper_device_id(bulb)
        try:
            if args.operation == "set":
                power = "on" if args.on_in is not None else "off"
                saved_timers[device_id] = auto.set_timer(client, bulb, power, args.on_in or args.off_in, state)
                row["end_time"] = saved_timers[device_id]["end_time"]
            else:
                auto.cancel_timer(client, bulb, args.timer_id)
                if saved_timers.get(device_id, {}).get("id") == args.timer_id:
                    del saved_timers[device_id]
            lights.save_state(client.account_id, state)
        except (ValueError, requests.RequestException) as error:
            error_message = lights.error_detail(error) if isinstance(error, requests.RequestException) else str(error)
            row.update(result="failed", error=error_message)
        results.append(row)
    display(args, results)
    return int(any(row["result"] == "failed" for row in results))


def activity(args, client, state):
    bulbs = lights.targets(client, args, state) if args.targets or args.all or args.group else client.bulbs()
    display(args, auto.activity(client, bulbs, args.days))


def firmware(args, client, state):
    bulbs = lights.targets(client, args, state) if args.targets or args.all or args.group else client.bulbs()
    display(args, [{key: row[key] for key in ("name", "model", "firmware", "wifi_status")}
                   for row in map(lights.summary, bulbs)])


def firmware_update(args, client, state):
    bulb = lights.select_bulb(client.bulbs(), args.bulb, state)
    reported = lights.summary(bulb)
    if args.dry_run:
        return display(args, {"name": reported["name"], "firmware": reported["firmware"],
                              "operation": "request vendor firmware update"})
    confirm(args, f"Request an official firmware update for {reported['name']!r}? Keep the bulb powered.")
    client.request("PUT", f"/account/devices/{lights.address(reported['id'])}/command/FirmwareUpdate")
    display(args, {"name": reported["name"], "result": "request accepted; installation not confirmed",
                   "current_firmware": reported["firmware"]})


def rename(args, client, state):
    bulb = lights.select_bulb(client.bulbs(), args.bulb, state)
    client.setting(bulb, "name", args.name)
    display(args, {"previous_name": device_name(bulb), "name": args.name, "result": "accepted"})


def pair(args, client, state):
    existing = lights.select_bulb(client.bulbs(), args.bulb, state) if args.command == "re-pair" else None
    display(args, pairing.pair(client, args, existing))


def unpair(args, client, state):
    bulb = lights.select_bulb(client.bulbs(), args.bulb, state)
    name = device_name(bulb)
    if args.dry_run:
        return display(args, {"name": name, "id": pepper_device_id(bulb), "operation": "remove from account"})
    confirm(args, f"Remove {name!r} from the Merkury Smart account?")
    result = pairing.unpair(client, bulb)
    display(args, result)
    return int(result["result"] != "removed from the account")


def doctor(args, client, _state):
    bulbs = client.bulbs()
    display(args, {"session": "connected", "backend": "Pepper online service", "bulbs": len(bulbs)})


def action_flags(parser):
    parser.add_argument("--dry-run", action="store_true", help="Preview the settings without sending them")
    parser.add_argument("--no-wait", action="store_true", help="Report acceptance without checking reported state")


def management(subparsers):
    for kind, description in (
        ("group", "Manage shared bulb groups"),
        ("scene", "Manage saved multi-device scenes"),
        ("rule", "Manage Pepper schedules and rules"),
    ):
        root = subparsers.add_parser(kind, help=description)
        operations = root.add_subparsers(dest="operation", required=True)
        operations_to_add = ["list", "show", "add", "edit", "delete"]
        if kind == "scene":
            operations_to_add.append("run")
        if kind == "rule":
            operations_to_add += ["enable", "disable"]
        for operation in operations_to_add:
            parser = command(operations, operation, f"{operation.capitalize()} {kind}s", entities)
            if operation != "list":
                parser.add_argument("name", type=nonempty, help=f"{kind.capitalize()} name or ID")
            if operation == "delete":
                destructive_flags(parser)
            if operation in ("add", "edit"):
                if operation == "edit":
                    parser.add_argument("--new-name", type=nonempty)
                if kind == "group":
                    parser.add_argument("members", nargs="*" if operation == "edit" else "+", metavar="BULB")
                else:
                    selection(parser)
                    parser.add_argument("--power", choices=("on", "off"))
                    mode_options(parser)
                    if kind == "scene":
                        parser.add_argument("--location", help="Pepper location ID, if applicable")
                    else:
                        parser.add_argument("--at", type=auto.time_offset, metavar="HH:MM")
                        parser.add_argument("--days", type=auto.week_days)
                        parser.add_argument("--scene", help="Run a saved scene when this rule fires")
                        parser.add_argument("--dry-run", action="store_true")


def make_parser():
    parser = argparse.ArgumentParser(prog="merkury", description=__doc__)
    parser.add_argument("--version", action="version", version=f"%(prog)s {version('merkury-cli')}")
    parser.add_argument("--json", action="store_true", help="Output JSON")
    subparsers = parser.add_subparsers(dest="command", required=True)

    entry = command(subparsers, "login", "Connect your Merkury Smart account in Chrome", login)
    entry.add_argument("--email", action="store_true", help="Use the Merkury Smart email and password instead")
    command(subparsers, "logout", "Remove the stored session from this Mac", logout)
    command(subparsers, "doctor", "Check your session and bulb access", doctor)
    command(subparsers, "list", "List paired bulbs", bulb_list, aliases=["devices"])
    selection(command(subparsers, "status", "Show reported bulb state", bulb_list))
    for name, value, help_text in (
        ("on", None, "Turn bulbs on"), ("off", None, "Turn bulbs off"),
        ("brightness", lights.bounded_int(1, 100), "Set brightness from 1 to 100"),
        ("color", lights.color_code, "Set an RGB color by name or hex"),
        ("white", lights.bounded_int(2700, 6500), "Set white temperature in Kelvin"),
    ):
        entry = command(subparsers, name, help_text, light_action, aliases=["colortemp"] if name == "white" else [])
        entry.set_defaults(action=name)
        selection(entry)
        if value:
            entry.add_argument("value", type=value, metavar="VALUE")
        action_flags(entry)
    presets = subparsers.add_parser("preset", help="List or apply built-in lighting presets").add_subparsers(
        dest="operation", required=True,
    )
    command(presets, "list", "List the nine bulb presets", preset_list)
    entry = command(presets, "apply", "Apply a built-in preset", light_action)
    selection(entry)
    entry.add_argument("value", choices=lights.PRESETS)
    entry.set_defaults(action="preset")
    action_flags(entry)
    recent = subparsers.add_parser("history", help="Show or reuse recent CLI colors").add_subparsers(
        dest="operation", required=True,
    )
    selection(command(recent, "colors", "List up to five recent CLI colors per bulb", history))
    entry = command(recent, "use", "Apply a color from the selected bulb’s history", light_action)
    entry.add_argument("targets", nargs=1, metavar="BULB")
    entry.add_argument("index", type=lights.bounded_int(1, 5))
    entry.set_defaults(action="reuse", all=False, group=None)
    action_flags(entry)

    aliases = subparsers.add_parser("alias", help="Manage local bulb shortcuts").add_subparsers(
        dest="operation", required=True,
    )
    command(aliases, "list", "List shortcuts", alias)
    entry = command(aliases, "set", "Set a shortcut for a bulb", alias)
    entry.add_argument("name", type=nonempty)
    entry.add_argument("bulb")
    entry = command(aliases, "remove", "Remove a shortcut", alias)
    entry.add_argument("name")

    management(subparsers)
    timers = subparsers.add_parser("timer", help="Manage countdown timers").add_subparsers(
        dest="operation", required=True,
    )
    for operation in ("list", "set", "cancel"):
        entry = command(timers, operation, f"{operation.capitalize()} countdown timers", timer)
        selection(entry)
        if operation == "set":
            when = entry.add_mutually_exclusive_group(required=True)
            when.add_argument("--on-in", type=auto.duration)
            when.add_argument("--off-in", type=auto.duration)
        if operation == "cancel":
            entry.add_argument("--timer-id", default="timer")

    entry = command(subparsers, "activity", "Show bulb activity recorded by Pepper", activity)
    selection(entry)
    entry.add_argument("--days", type=lights.bounded_int(1, 30), default=1)
    entry = command(subparsers, "rename", "Change a bulb’s account name", rename)
    entry.add_argument("bulb")
    entry.add_argument("name", type=nonempty)
    firmware_commands = subparsers.add_parser("firmware", help="Inspect bulb firmware").add_subparsers(
        dest="operation", required=True,
    )
    selection(command(firmware_commands, "status", "Show reported firmware versions", firmware))
    entry = command(firmware_commands, "update", "Request an official firmware update (experimental)", firmware_update)
    entry.add_argument("bulb")
    destructive_flags(entry)

    for name in ("pair", "re-pair"):
        entry = command(subparsers, name, "Provision a BW950 bulb using its setup hotspot", pair)
        if name == "re-pair":
            entry.add_argument("bulb")
            entry.set_defaults(name=None, device_id=None)
        else:
            entry.add_argument("--name", type=nonempty)
            entry.add_argument("--device-id", help="Expected hardware ID from the setup hotspot")
        entry.add_argument("--ssid", help="Home Wi-Fi name. The password is entered separately.")
        entry.add_argument("--wait", type=lights.bounded_int(5, 600), default=120, metavar="SECONDS")
    entry = command(subparsers, "unpair", "Remove a specific bulb from your account", unpair)
    entry.add_argument("bulb")
    destructive_flags(entry)

    command(subparsers, "completion", "Print shell-completion setup", completion).add_argument(
        "shell", choices=("zsh", "bash"), nargs="?", default="zsh",
    )
    return parser


def preset_list(args, _client, _state):
    display(args, list(lights.PRESETS))


def completion(args, _client, _state):
    from argcomplete import shellcode

    if args.shell == "zsh":
        print("if ! (( $+functions[compdef] )); then autoload -Uz compinit; compinit -D; fi")
    print(shellcode(["merkury"], shell=args.shell), end="")


def main():
    parser = make_parser()
    argcomplete.autocomplete(parser)
    args = parser.parse_args()
    try:
        if args.handler in (login, logout, completion, preset_list):
            return args.handler(args, None, {})
        client = Pepper.connect()
        return args.handler(args, client, lights.load_state(client.account_id))
    except (ValueError, OSError, keyring.errors.KeyringError) as error:
        message = str(error)
    except requests.RequestException as error:
        message = lights.error_detail(error)
    except BrowserError:
        message = "Chrome sign-in failed or was closed. Open Chrome and run merkury login again."
    except (KeyError, TypeError):
        message = "Pepper returned a response this version does not recognize."
    except (KeyboardInterrupt, EOFError):
        message = "Cancelled."
    if args.json:
        display(args, {"error": message})
    else:
        print(f"merkury: {message}", file=sys.stderr)
    return 1
