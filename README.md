# Merkury CLI

Control Merkury Smart bulbs from macOS Terminal without opening the Merkury Smart app. The CLI uses your Merkury Smart account and Pepper’s online service. Lighting controls were tested on the `MI-BW950-999W-A2`.

## Install on macOS

Install [uv](https://docs.astral.sh/uv/getting-started/installation/) if you don’t have it. If you use Homebrew, run `brew install uv`. The CLI requires Python 3.10 or later, which uv can install for you. Install Google Chrome if you plan to sign in through the browser.

```sh
uv tool install git+https://github.com/moosefroggo/merkury-cli.git
merkury --version
```

If Terminal can’t find `merkury`, run `uv tool update-shell` and open a new Terminal window.

## Sign in and get started

```sh
merkury login
merkury list
merkury on "Porch"
merkury off --all
```

`merkury login` opens the official Merkury Smart sign-in page in Chrome. Choose **Sign in with Apple** there. If your account uses an email and password, run `merkury login --email`. The session is stored in macOS Keychain. If you’re already signed in, start with `merkury list`. Substitute a name from that list for `Porch` in the examples.

## Command reference

| Task | Commands |
| --- | --- |
| Find bulbs and check their state | `list`, `status`, `doctor` |
| Control lighting | `on`, `off`, `brightness`, `color`, `white`, `preset` |
| Reuse settings and shorten names | `history`, `alias` |
| Automate lighting | `group`, `scene`, `rule`, `timer` |
| Manage bulbs | `activity`, `rename`, `location`, `firmware` |
| Set up or remove bulbs (experimental) | `pair`, `re-pair`, `unpair` |
| Manage your session and shell | `login`, `logout`, `completion` |

Run `merkury --help` for the full list or `merkury rule add --help` for a specific command. Use `--json` with supported commands for scripts.

## Control bulbs

```sh
merkury status --all
merkury on "Porch"
merkury off --all
merkury brightness "Porch" 40
merkury color "Porch" amber
merkury white "Porch" 2700
merkury preset list
merkury preset apply "Porch" reading
merkury history colors "Porch"
merkury history use "Porch" 1
merkury history white "Porch"
merkury history use "Porch" 1 --category white
merkury alias set porch "Porch"
merkury status porch --json
```

Commands accept exact names, aliases, or IDs from `merkury list`. To control several bulbs, pass multiple names, `--all`, or `--group NAME`. Brightness uses 1–100 and white temperature uses 2700–6500 K. Power has its own commands. Lighting commands support `--dry-run` and normally check Pepper's reported state after sending the command. `--no-wait` skips readback. The history holds up to five colors and white temperatures set through this CLI on this Mac.

## Groups, scenes, rules, and timers

```sh
merkury group add Entry "Porch" "Hall"
merkury brightness --group Entry 50
merkury scene add Evening --group Entry --power on --brightness 40 --white 2700
merkury scene run Evening
merkury rule add "Weeknight lights" --group Entry --at 18:30 --days Mon,Tue,Wed,Thu,Fri --power on
merkury rule show "Weeknight lights"
merkury rule disable "Weeknight lights"
merkury timer set "Porch" --off-in 30m
merkury timer list
merkury timer cancel "Porch"
merkury activity "Porch" --days 7
```

Pepper runs rules and countdown timers even when the Mac is asleep. Check the new schedule with `merkury rule list`. `group`, `scene`, and `rule` have `list`, `show`, `add`, `edit`, and `delete` commands. Use `--help` on any command for options. Timer listings can show local receipts for countdowns submitted from this Mac because Pepper does not always return them in the bulb record.

## Setup and device management

```sh
merkury rename "Porch" "Front Porch"
merkury location list
merkury location add "Second Home"
merkury location move "Second Home" "Front Porch"
merkury firmware status --all
merkury firmware update "Front Porch" --dry-run
merkury pair --name "Hall"
merkury re-pair "Front Porch"
merkury unpair "Front Porch" --dry-run
```

Pairing walks you through the BW950 setup hotspot. Join it from the Mac Wi-Fi menu when prompted and return to home Wi-Fi afterward. Use a 2.4 GHz network and enter its password in the hidden Terminal prompt. Re-pair checks the hardware ID before sending settings. Unpair removes a named bulb from your account and shows the physical reset instructions. Pairing, account removal, and starting a firmware update are experimental until tested on a spare BW950-A2. The firmware command requests a vendor update; local firmware flashing is a different operation.

## Shell completion

Run this for the current zsh session, or add it to `~/.zshrc` to enable completion in new Terminal windows.

```zsh
source <(merkury completion zsh)
```

## Update or uninstall

```sh
uv tool upgrade --refresh merkury-cli
```

To remove the saved session and uninstall the CLI, run:

```sh
merkury logout
uv tool uninstall merkury-cli
```
