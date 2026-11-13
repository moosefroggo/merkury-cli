# /// script
# requires-python = ">=3.10"
# dependencies = ["keyring", "playwright", "requests"]
# ///
"""Control Merkury bulbs through the Pepper service from Terminal."""

import argparse
import base64
import hashlib
import hmac
import json
import re
import sys
from datetime import datetime, timezone
from getpass import getpass
from tempfile import TemporaryDirectory
from urllib.parse import quote, urlsplit

import keyring
import requests
from playwright.sync_api import TimeoutError as BrowserTimeout, sync_playwright

HOST = "api.pepperos.io"
BRAND = "geeni"
KEYCHAIN_SERVICE = "Merkury Bulb Control"
KEYCHAIN_ACCOUNT = "Pepper sign-in token"
HEADERS = {"Accept": "application/json", "Content-Type": "application/json"}
ACCOUNT_LOGIN = "https://account.merkurysmart.com/login"


def pepper_request(method, path, **kwargs):
    response = requests.request(
        method, f"https://{HOST}{path}", timeout=12, allow_redirects=False, **kwargs
    )
    if not 200 <= response.status_code < 300:
        raise requests.HTTPError(f"HTTP {response.status_code}", response=response)
    return response


def authenticate(mode, credential):
    basic = base64.b64encode(f"{BRAND}:{credential}".encode()).decode()
    return pepper_request(
        "POST", f"/authentication/{mode}", headers={**HEADERS, "Authorization": f"Basic {basic}"}
    ).json()


def sign_in():
    if not sys.stdin.isatty():
        raise ValueError("Open Terminal and run the login command to sign in privately.")

    email = input("Merkury Smart email: ").strip()
    password = getpass("Merkury Smart password: ")
    profile = authenticate("byEmail", f"{email}:{password}")
    mfa = profile.get("mfaLoginInfo") or {}
    if mfa.get("secondFactorAuthenticationToken"):
        profile = pepper_request(
            "POST",
            "/authentication/mfaValidateLoginOTP",
            headers=HEADERS,
            json={
                "brand": BRAND,
                "email": email,
                "oneTimePasscode": getpass("Verification code: ").strip(),
                "secondFactorAuthenticationToken": mfa["secondFactorAuthenticationToken"],
                "userMFASettings": mfa,
            },
        ).json()
    return profile


def is_web_sign_in(response):
    url = urlsplit(response.url)
    return (
        response.ok
        and response.request.method == "POST"
        and url.scheme == "https"
        and url.hostname == HOST
        and url.path == f"/authentication/authenticateViaSocial/{BRAND}"
    )


def web_sign_in():
    with TemporaryDirectory(prefix="merkury-sign-in-") as directory, sync_playwright() as browser:
        context = browser.chromium.launch_persistent_context(directory, channel="chrome", headless=False)
        try:
            page = context.pages[0]
            print("Choose Sign in with Apple on the Merkury Smart page in Chrome.", flush=True)
            with context.expect_event("response", predicate=is_web_sign_in, timeout=480_000) as signed_in:
                page.goto(ACCOUNT_LOGIN, wait_until="domcontentloaded")
            return signed_in.value.json()
        except BrowserTimeout:
            raise ValueError("Browser sign-in timed out. Run connect-web to try again.") from None
        finally:
            context.close()


def signed_headers(profile, method, path, body=b""):
    aws = profile["pepperUser"]["awsUserCredentials"]
    region = aws.get("Region") or aws.get("region") or "us-east-1"
    now = datetime.now(timezone.utc)
    timestamp = now.strftime("%Y%m%dT%H%M%SZ")
    date = now.strftime("%Y%m%d")
    headers = {
        "accept": "application/json",
        "host": HOST,
        "peppertoken": profile["token"],
        "x-amz-date": timestamp,
        "x-amz-security-token": aws["SessionToken"],
    }
    signed = ";".join(headers)
    canonical_headers = "".join(f"{name}:{value}\n" for name, value in headers.items())
    canonical_request = "\n".join((
        method, path, "", canonical_headers, signed, hashlib.sha256(body).hexdigest(),
    ))
    scope = f"{date}/{region}/execute-api/aws4_request"
    to_sign = "\n".join((
        "AWS4-HMAC-SHA256", timestamp, scope, hashlib.sha256(canonical_request.encode()).hexdigest(),
    ))
    key = ("AWS4" + aws["SecretAccessKey"]).encode()
    for part in (date, region, "execute-api", "aws4_request"):
        key = hmac.digest(key, part.encode(), "sha256")
    signature = hmac.digest(key, to_sign.encode(), "sha256").hex()
    headers["Authorization"] = (
        f"AWS4-HMAC-SHA256 Credential={aws['AccessKeyId']}/{scope}, "
        f"SignedHeaders={signed}, Signature={signature}"
    )
    if body:
        headers["Content-Type"] = "application/json"
    return headers


def list_devices(profile):
    path = "/account/devices"
    data = pepper_request("GET", path, headers=signed_headers(profile, "GET", path)).json()
    devices = data if isinstance(data, list) else data["devices"]
    return [device for device in devices if not device.get("deleted")]


def device_name(device):
    return device.get("name") or (device.get("settings") or {}).get("deviceName") or "Unnamed device"


def pepper_device_id(device):
    return device.get("pepperDeviceId") or device.get("id") or device["deviceId"]


def run(args):
    connecting = args.command in ("login", "connect-web")
    if args.command == "login":
        profile = sign_in()
    elif args.command == "connect-web":
        profile = web_sign_in()
    else:
        token = keyring.get_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT)
        if not token:
            raise ValueError("Run connect-web or login to connect your Merkury Smart account.")
        try:
            profile = authenticate("byToken", token)
        except requests.HTTPError as error:
            if error.response.status_code in (401, 403):
                raise ValueError("Pepper rejected the saved session. Reconnect your Merkury Smart account.") from None
            raise

    devices = list_devices(profile)
    if connecting:
        token = profile.get("lrToken")
        if not token:
            raise ValueError("Pepper signed you in but did not return a reusable sign-in token.")
        keyring.set_password(KEYCHAIN_SERVICE, KEYCHAIN_ACCOUNT, token)
        print("Signed in. Your sign-in token is saved in macOS Keychain.")

    if connecting or args.command == "devices":
        if not devices:
            print("No devices found in this account.")
        for device in devices:
            print(f"{device_name(device)}  ({pepper_device_id(device)})")
        return

    matches = [
        device for device in devices
        if args.device in (pepper_device_id(device), device.get("deviceId"))
        or args.device.casefold() == device_name(device).casefold()
    ]
    if len(matches) != 1:
        raise ValueError("Use an exact name or a unique device ID from the devices command.")

    device = matches[0]
    setting, value = {
        "on": ("light.stateOn", 1),
        "off": ("light.stateOn", 0),
        "brightness": ("light.brightness", args.value),
        "color": ("light.color", args.value),
        "colortemp": ("light.colorTemp", args.value),
    }[args.command]
    device_id = quote(pepper_device_id(device), safe="")
    path = f"/account/devices/{device_id}/settings/{setting}"
    body = json.dumps({"valueJson": json.dumps(value)}).encode()
    pepper_request("PUT", path, headers=signed_headers(profile, "PUT", path, body), data=body)
    print(f"Pepper accepted the {args.command} command for {device_name(device)}.")


def color_code(value):
    value = value.removeprefix("#").upper()
    if not re.fullmatch(r"[0-9A-F]{6}", value):
        raise argparse.ArgumentTypeError("Enter a six-digit color such as FF0000.")
    return value


def main():
    parser = argparse.ArgumentParser(description=__doc__)
    commands = parser.add_subparsers(dest="command", required=True)
    commands.add_parser("login", help="Sign in and list devices")
    commands.add_parser("connect-web", help="Connect with Sign in with Apple in Chrome")
    commands.add_parser("devices", help="List devices")
    for command in ("on", "off", "brightness", "color", "colortemp"):
        control = commands.add_parser(command)
        control.add_argument("device", help="Exact device name or ID")
        if command == "brightness":
            control.add_argument("value", type=int, choices=range(101), metavar="0-100")
        if command == "color":
            control.add_argument("value", type=color_code, metavar="RRGGBB")
        if command == "colortemp":
            control.add_argument("value", type=int, choices=range(2700, 6501), metavar="2700-6500")
    parser.set_defaults(value=None)
    try:
        run(parser.parse_args())
    except (ValueError, keyring.errors.KeyringError) as error:
        parser.exit(1, f"{error}\n")
    except requests.RequestException as error:
        status = f" (HTTP {error.response.status_code})" if error.response is not None else ""
        parser.exit(1, f"Pepper could not complete the request{status}.\n")
    except (KeyError, TypeError):
        parser.exit(1, "Pepper returned an unexpected response.\n")
    except (KeyboardInterrupt, EOFError):
        parser.exit(1, "\nCancelled.\n")


if __name__ == "__main__":
    main()
