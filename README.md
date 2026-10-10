# spacefix

Finish a failed iPhone update **without erasing your data**, when Finder fails with
**error 100 (06A6.0064)** because the iPhone ran out of storage during the update.

> ⚠️ **Unofficial and experimental.** This automates a community method. It runs an
> *update* (Apple's keep-data mode), never an erase, but nothing about it is
> supported by Apple and nothing is guaranteed. If you have a backup, restoring
> from it is the safer route.

## Is this your problem?

All of these should be true:

- An iOS update through Finder (or over the air) failed and the iPhone is stuck in
  **Recovery Mode** (computer + cable screen).
- Finder shows **"The iPhone could not be updated. An unknown error occurred (100).
  06A6.0064"**, or Apple Configurator shows **"AMRestoreErrorDomain error 100 … failed
  to install splat"**.
- The iPhone's storage was nearly full.
- Retrying in Finder, Apple Configurator "Revive", or 3uTools "Keep data" fails at the
  same point every time.

`spacefix.py diagnose` checks Finder's own logs and only says yes when it finds:

```
Transfer: Failed preallocation of 6031409152 bytes for /mnt9/cryptex1/current/os.dmg
Transfer: Failed preallocation due to space. No hope in this succeeding, bailing.
CHECKPOINT FAILURE:(FAILURE:100) … [0x06A6] install_splat
```

## Why it fails, and what spacefix changes

Before installing, the iPhone checks that it has enough room. With a nearly full
phone that check can pass by a few hundred MB. Then, near the end, the 6 GB
`Cryptex1,SystemOS` image does not fit and the update aborts.

spacefix raises `MinimumSystemPartition` (by 1387 MB by default) in the IPSW's
**Upgrade** build identity only. With the higher requirement the device makes room
before it starts, and the cryptex then fits. Everything else is unchanged: same
Apple-signed iOS, same keep-data update path.

## Requirements

- A Mac (Apple Silicon or Intel) with [Homebrew](https://brew.sh)
- Python 3.9+ (the one that ships with macOS is fine)
- About 25 GB free (12 GB IPSW download plus 12 GB extracted)
- A good USB cable, plugged **directly** into the Mac

## Usage

```bash
# 1. Check that Finder's logs show this exact failure
python3 spacefix.py diagnose

# 2. Build idevicerestore + libtatsu into ~/.spacefix (once, a few minutes)
python3 spacefix.py setup

# 3. Put the iPhone in Recovery Mode, connect it, then check everything
python3 spacefix.py doctor

# 4. Rehearse: does everything except send anything to the iPhone
python3 spacefix.py run --dry-run

# 5. Run it for real (asks you to type KEEP MY DATA first)
python3 spacefix.py run
```

Useful options for `run`:

| Option | Meaning |
|---|---|
| `--ipsw PATH` | Use an IPSW you already downloaded (it must still be signed) |
| `--extracted DIR` | Use an already extracted IPSW folder |
| `--delta MB` | Raise to apply (default 1387). Try a higher value if it still fails at install_splat |
| `--dry-run` | Prepare and print the command, send nothing |

While it runs, don't touch the phone, cable or Mac. Apple's "iPhone Updater" window
is closed automatically, because it can take over the USB connection mid-transfer.
After "Restore finished" the iPhone restarts, may show a progress bar or
"Attempting Data Recovery", and then the Lock Screen. **Back up immediately, then
free at least 10 GB.**

## Safety measures

- No erase flag (`-e` / `--erase`) is ever passed. Building a command with one, or
  with an Erase variant, raises an error. This is covered by tests.
- The run is aborted if `idevicerestore` does not announce *"update the device
  without erasing user data"* before it sends anything.
- Exactly one Upgrade/Update identity for your board must exist, or nothing runs.
- Only that identity is changed. The original manifest is kept as
  `BuildManifest.original.plist`, and re-runs always start from it.
- The USB connection must stay up for 10 seconds before starting.
- `setup` installs only missing Homebrew packages and never upgrades existing ones.

## Troubleshooting

| Message | What to do |
|---|---|
| `Broken pipe` / `Unable to find device` | The USB connection dropped. Use another cable or port, directly on the Mac, force-restart the phone into Recovery Mode, run again |
| `Unable to fetch Yonkers ticket` | libtatsu is too old. Run `python3 spacefix.py setup` again |
| Still fails at install_splat | Run again with a larger `--delta` (for example 2500) |
| Phone stuck on Apple logo + bar after a failure | Force-restart: Volume Up, Volume Down, hold Side until the computer + cable screen appears |

Logs are kept in `~/.spacefix/logs/`.

## Credits

- Method: u/CursumPerficioX, *"[GUIDE] Fixed iPhone Error 100 / 06A6 install_splat
  without losing data"* (r/applehelp), plus the people who confirmed it in the comments.
- [idevicerestore](https://github.com/libimobiledevice/idevicerestore) (LGPL-3.0) and
  [libtatsu](https://github.com/libimobiledevice/libtatsu) (LGPL-2.1) by the
  libimobiledevice project, including the Yonkers fix
  ([libtatsu#8](https://github.com/libimobiledevice/libtatsu/pull/8)). spacefix builds
  them from source on your machine and does not redistribute them.

## License

MIT. See [LICENSE](LICENSE). Not affiliated with or endorsed by Apple.
