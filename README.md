# Merkury CLI

Control Merkury Smart bulbs from a Mac terminal using your existing Merkury Smart account and Pepper's online service.

```sh
uv tool install .
merkury list
merkury login
```

If you already signed in through the previous bulb script, `merkury list` can reuse that macOS Keychain entry. Otherwise, `merkury login` opens the official sign-in page in Chrome. Use `merkury logout` to remove the stored session from this Mac.

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

Groups, scenes, rules, and countdown timers use Pepper and keep running when the Mac is asleep. Rules use the same scheduled-time fields as the Merkury app. Check the new schedule with `rule list`. `group`, `scene`, and `rule` have `list`, `show`, `add`, `edit`, and `delete` commands. Use `--help` on any command for options. Timer listings can show local receipts for countdowns submitted from this Mac because Pepper does not always return them in the bulb record.

## Setup and device management

```sh
merkury rename "Porch" "Front Porch"
merkury firmware status --all
merkury firmware update "Front Porch" --dry-run
merkury pair --name "Hall"
merkury re-pair "Front Porch"
merkury unpair "Front Porch" --dry-run
```

Pairing walks you through the BW950 setup hotspot. Join it from the Mac Wi-Fi menu when prompted and return to home Wi-Fi afterward. Use a 2.4 GHz network and enter its password in the hidden Terminal prompt. Re-pair checks the hardware ID before sending settings. Unpair removes a named bulb from your account and shows the physical reset instructions. Pairing, account removal, and starting a firmware update are experimental until tested on a spare BW950-A2. The firmware command requests a vendor update; local firmware flashing is a different operation.

To enable command completion in zsh, evaluate the output of `merkury completion zsh` in your shell startup file. Use `merkury doctor` to check account connectivity.
