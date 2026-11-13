"""Merkury Smart's BW950 setup hotspot and account registration."""

import base64
import json
import re
import secrets
import socket
import sys
from getpass import getpass
from time import monotonic, sleep

import requests
from cryptography.hazmat.primitives import padding
from cryptography.hazmat.primitives.ciphers import Cipher, algorithms, modes

from .lighting import address, identifiers
from .pepper import BRAND, device_name, pepper_device_id

# BW950's access point settings and the production Mersoft server from Merkury's configuration.
HOTSPOT = ("192.168.8.1", 5053)
SERVER = "wss://prod.move.pepperos.io/ws"
RESET = "Turn the bulb off and on three times at the wall switch, then check that it is flashing."


def prompt(message):
    return input(message).strip()


def encrypted_wifi(seed, mac, external_id, ssid, password):
    key = bytearray()
    for start, mac_index in zip((0, 4, 8, 12, 22, 26), range(5, -1, -1)):
        key.extend(seed[start:start + 4])
        key.append(mac[mac_index])
    key.extend(seed[30:32])
    iv = external_id[-16:].encode().ljust(16, b"\0")
    inner = json.dumps({"ssid": ssid, "password": password, "deviceID": external_id},
                       ensure_ascii=False, separators=(",", ":")).encode()
    padder = padding.PKCS7(128).padder()
    encryptor = Cipher(algorithms.AES(bytes(key)), modes.CBC(iv)).encryptor()
    data = encryptor.update(padder.update(inner) + padder.finalize()) + encryptor.finalize()
    return base64.b64encode(data).decode()


class SetupSocket:
    def __init__(self, connection):
        self.connection = connection
        self.buffer = b""

    def call(self, command, **fields):
        self.connection.sendall(json.dumps({"req": command, **fields}, separators=(",", ":")).encode())
        deadline = monotonic() + 12
        while monotonic() < deadline:
            try:
                text = self.buffer.decode()
                message, end = json.JSONDecoder().raw_decode(text.lstrip())
            except (ValueError, UnicodeError):
                self.connection.settimeout(max(0.1, deadline - monotonic()))
                chunk = self.connection.recv(4096)
                if not chunk or len(self.buffer) + len(chunk) > 65_536:
                    break
                self.buffer += chunk
                continue
            self.buffer = text.lstrip()[end:].encode()
            if not isinstance(message, dict) or message.get("res") != command:
                continue
            if message.get("success") not in (True, "true"):
                raise ValueError(f"The bulb rejected the {command!r} step.")
            return message
        raise TimeoutError(f"The bulb did not acknowledge {command!r}.")


def provision(ssid, password, account, expected_id):
    with socket.create_connection(HOTSPOT, timeout=10) as connection:
        setup = SetupSocket(connection)
        identity = setup.call("get device id")
        external_id = identity.get("value")
        raw_mac = str(identity.get("macID") or "").replace(":", "").replace("-", "")
        if not isinstance(external_id, str) or not re.fullmatch(r"psv-[0-9a-fA-F]{12}", external_id):
            raise ValueError("This hotspot did not identify itself as a supported BW950 bulb.")
        if identity.get("provider") not in (None, "", "mersoft"):
            raise ValueError("This bulb uses a different provisioning provider.")
        if expected_id and external_id.casefold() != expected_id.casefold():
            raise ValueError("The hotspot belongs to a different bulb. Reconnect and try again.")
        if raw_mac and (not re.fullmatch(r"[0-9a-fA-F]{12}", raw_mac)
                        or raw_mac.casefold() != external_id[4:].casefold()):
            raise ValueError("The bulb returned conflicting Wi-Fi identifiers.")
        if prompt(f"Bulb ID ends in {external_id[-4:]}. Provision this bulb? [y/N] ").casefold() not in ("y", "yes"):
            raise ValueError("Pairing cancelled.")

        seed = secrets.token_bytes(32) if raw_mac else None
        fields = {"value": account}
        if seed:
            fields["data"] = seed.hex()
        if seed:
            wifi = {"data": encrypted_wifi(seed, bytes.fromhex(raw_mac), external_id, ssid, password)}
        else:
            # Older BW950 firmware uses a character shift for these two fields.
            if prompt("This bulb's setup protocol does not encrypt Wi-Fi credentials. Continue? [y/N] ").casefold() not in ("y", "yes"):
                raise ValueError("Pairing cancelled.")
            wifi = {key: "".join(chr(ord(char) + 4) for char in value)
                    for key, value in {"ssid": ssid, "password": password}.items()}
        setup.call("set account id", **fields)
        sleep(0.5)
        try:
            setup.call("set server", value=SERVER)
        except ValueError:
            setup.call("set server", value=SERVER.replace("pepperos", "pepperio", 1))
        sleep(0.5)
        acknowledged = True
        try:
            setup.call("set wifi", **wifi)
        except OSError:
            # The access point can disappear as it switches to the home network.
            acknowledged = False
        return external_id, acknowledged


def pair(client, args, existing=None):
    if not sys.stdin.isatty() or args.json:
        raise ValueError("Pairing needs an interactive Terminal window.")
    account = client.account_id
    ssid = args.ssid or prompt("Home Wi-Fi name (2.4 GHz): ")
    if not 1 <= len(ssid.encode()) <= 32:
        raise ValueError("The Wi-Fi name must be between 1 and 32 bytes.")
    password = getpass("Home Wi-Fi password: ")
    if password and not (8 <= len(password.encode()) <= 63 or re.fullmatch(r"[0-9a-fA-F]{64}", password)):
        raise ValueError("Use an 8–63 character Wi-Fi password or a 64-digit hexadecimal key.")
    expected = existing.get("deviceId") if existing else args.device_id
    if existing and not expected:
        raise ValueError("Pepper did not return a hardware ID for this bulb.")
    previous = {str(bulb.get("deviceId", "")).casefold(): bulb for bulb in client.bulbs()}
    print(RESET)
    prompt("Join this bulb’s MKRY… (or Merkury…) hotspot in the Mac Wi-Fi menu, then press Enter. ")
    try:
        external_id, acknowledged = provision(ssid, password, account, expected)
    except OSError:
        raise ValueError("Could not talk to the bulb at its setup hotspot. Rejoin its Wi-Fi and try again.") from None
    print("The bulb accepted its Wi-Fi settings." if acknowledged else "The hotspot disconnected during setup.")
    prompt("Reconnect this Mac to your home Wi-Fi, then press Enter. ")

    deadline = monotonic() + args.wait
    while True:
        try:
            devices = client.bulbs()
        except requests.RequestException:
            devices = []
        for bulb in devices:
            ids = {str(identifier).casefold() for identifier in identifiers(bulb) if identifier}
            if external_id.casefold() not in ids or not registration_confirmed(bulb, previous.get(external_id.casefold()), ssid):
                continue
            wifi_status = str((bulb.get("wifi") or {}).get("status") or "").upper()
            if existing is None and args.name and device_name(bulb) != args.name:
                client.setting(bulb, "name", args.name)
            return {
                "result": "registered; Wi-Fi status " + (wifi_status or "not reported"),
                "name": args.name if existing is None and args.name else device_name(bulb),
                "device_id": pepper_device_id(bulb),
            }
        if monotonic() >= deadline:
            break
        sleep(3)
    raise ValueError("Pepper did not confirm registration on the new Wi-Fi. Run merkury status before retrying.")


def registration_confirmed(bulb, previous, ssid):
    wifi = bulb.get("wifi") or {}
    if str(bulb.get("status") or "").upper() != "ATTACHED":
        return False
    if str(wifi.get("status") or "").upper() in {"OFFLINE", "DISCONNECTED"}:
        return False
    if wifi.get("wifiNetworkName") not in (None, "", ssid):
        return False
    if previous is None:
        return True
    old_wifi = previous.get("wifi") or {}
    return (
        wifi.get("wifiNetworkName") == ssid != old_wifi.get("wifiNetworkName")
        or bool(bulb.get("lastPairedAt") and bulb["lastPairedAt"] != previous.get("lastPairedAt"))
    )


def unpair(client, bulb):
    device_id = pepper_device_id(bulb)
    provider = bulb.get("provider")
    if not provider:
        raise ValueError("Pepper did not return a provider for this bulb.")
    path = f"/account/devices/{address(device_id)}/{BRAND}/{address(provider)}"
    client.request("DELETE", path)
    deadline = monotonic() + 8
    while any(pepper_device_id(current) == device_id for current in client.bulbs()):
        if monotonic() >= deadline:
            return {"name": device_name(bulb), "result": "removal accepted, account state unconfirmed"}
        sleep(0.7)
    return {"name": device_name(bulb), "result": "removed from the account", "reset": RESET}
