#!/usr/bin/env python3
"""
spacefix: finish a failed iPhone update without erasing data, when it fails
with "error 100 / 06A6 install_splat" because the phone is short of storage.

What it does (macOS only):
  1. diagnose  - confirms, from Finder's own restore logs, that the failure is
                 "Failed preallocation due to space" at install_splat.
  2. setup     - builds idevicerestore + libtatsu (with the Yonkers fix) into
                 ~/.spacefix/prefix.
  3. run       - finds the iPhone in Recovery Mode, gets the signed IPSW,
                 raises MinimumSystemPartition in the *Upgrade* build identity
                 only, and runs idevicerestore in update (keep-data) mode.

There is deliberately no way to erase the device with this script: the erase
flag is never passed, and the run is aborted if idevicerestore does not
announce an update-without-erasing restore.

This is an unofficial, community-derived method. Use at your own risk.
Python 3.9+, standard library only.
"""

import argparse
import hashlib
import json
import os
import plistlib
import re
import shutil
import signal
import subprocess
import sys
import threading
import time
import urllib.request
import zipfile
from pathlib import Path
from typing import Dict, Iterator, List, Optional

VERSION = "0.1.0"

HOME = Path.home()
FINDER_LOG_DIR = HOME / "Library/Logs/iPhone Updater Logs"
SPACEFIX_HOME = Path(os.environ.get("SPACEFIX_HOME", HOME / ".spacefix"))
PREFIX = SPACEFIX_HOME / "prefix"
SRC_DIR = SPACEFIX_HOME / "src"
CACHE_DIR = SPACEFIX_HOME / "cache"
WORK_DIR = SPACEFIX_HOME / "work"
LOG_DIR = SPACEFIX_HOME / "logs"
IDEVICERESTORE = PREFIX / "bin/idevicerestore"

# Raise applied to MinimumSystemPartition (MB). This is the value every
# reported success used (e.g. 8691->10078, 8569->9956, 9379->10766).
DEFAULT_DELTA_MB = 1387

# Known-good upstream commits (the combination that fixed an iPhone 15 Pro Max
# on iOS 27.0.1). libtatsu cdbca26 is the Yonkers,SysTopPatch fix.
LIBTATSU_REPO = "https://github.com/libimobiledevice/libtatsu.git"
LIBTATSU_COMMIT = "e7d6ad1"
IDEVICERESTORE_REPO = "https://github.com/libimobiledevice/idevicerestore.git"
IDEVICERESTORE_COMMIT = "4b3e847"

BREW_DEPS = [
    "libplist", "libimobiledevice-glue", "libimobiledevice", "libirecovery",
    "libzip", "openssl@3", "curl", "autoconf", "automake", "libtool",
    "pkgconf",
]

UPDATER_APP = "MobileDeviceUpdater"
CONFIRM_PHRASE = "KEEP MY DATA"


# --------------------------------------------------------------------------
# Output helpers
# --------------------------------------------------------------------------

def info(msg: str) -> None:
    print(f"  {msg}", flush=True)


def ok(msg: str) -> None:
    print(f"  [ok] {msg}", flush=True)


def warn(msg: str) -> None:
    print(f"  [!]  {msg}", flush=True)


def step(title: str) -> None:
    print(f"\n== {title} ==", flush=True)


class SpaceFixError(Exception):
    """A problem that stops the run with an explanation for the user."""


def run_cmd(cmd: List[str], **kw) -> subprocess.CompletedProcess:
    return subprocess.run(cmd, text=True, capture_output=True, **kw)


def free_bytes(path: Path) -> int:
    path.mkdir(parents=True, exist_ok=True)
    return shutil.disk_usage(path).free


def gb(n: float) -> str:
    return f"{n / 1e9:.1f} GB"


# --------------------------------------------------------------------------
# 1. Diagnose from Finder's restore logs
# --------------------------------------------------------------------------

PREALLOC_RE = re.compile(
    r"Failed preallocation of (\d+) bytes for (\S*cryptex\S*)", re.I)
MINSYS_RE = re.compile(r"minSystemPartitionSize\s*:\s*(\d+)\s*MB")
SPACE_CHECK_RE = re.compile(
    r"SpaceNeededForRestore:\s*(\d+)\s*MB\s*ContainerFreeSpace:\s*(\d+)\s*MB")
FREE_AFTER_WIPE_RE = re.compile(
    r"Space usage: DataVolumeSpaceUsed:\s*\d+\s*MB\s*ContainerFreeSpace:\s*(\d+)\s*MB")
SYSTEM_IMAGE_RE = re.compile(r"apfs_invert_asr_img: dstream_id=\d+, size=(\d+)")
SPLAT_FAIL_RE = re.compile(r"CHECKPOINT FAILURE:\(FAILURE:100\).*install_splat")


def parse_restore_log(text: str) -> Dict:
    """Extract the space-failure evidence from one Finder restore log."""
    result: Dict = {
        "space_failure": False,
        "splat_failure": bool(SPLAT_FAIL_RE.search(text)),
        "prealloc_bytes": None,
        "min_system_partition_mb": None,
        "space_needed_mb": None,
        "free_before_mb": None,
        "free_after_wipe_mb": None,
        "system_image_bytes": None,
    }
    m = PREALLOC_RE.findall(text)
    if m and "Failed preallocation due to space" in text:
        result["space_failure"] = True
        result["prealloc_bytes"] = int(m[-1][0])
    if (m := MINSYS_RE.findall(text)):
        result["min_system_partition_mb"] = int(m[-1])
    if (m := SPACE_CHECK_RE.findall(text)):
        result["space_needed_mb"], result["free_before_mb"] = map(int, m[-1])
    if (m := FREE_AFTER_WIPE_RE.findall(text)):
        result["free_after_wipe_mb"] = int(m[-1])
    if (m := SYSTEM_IMAGE_RE.findall(text)):
        result["system_image_bytes"] = int(m[-1])
    return result


def estimate_shortfall_mb(ev: Dict) -> Optional[int]:
    """Space missing at the cryptex step: what it needed minus what was left
    after the new system image was written. None if the log lacks the data."""
    if not (ev["prealloc_bytes"] and ev["free_after_wipe_mb"]
            and ev["system_image_bytes"]):
        return None
    left_mb = ev["free_after_wipe_mb"] - ev["system_image_bytes"] / 1e6
    return int(ev["prealloc_bytes"] / 1e6 - left_mb)


def diagnose(log_dir: Path = FINDER_LOG_DIR) -> Dict:
    logs = sorted(log_dir.glob("*.log"), key=lambda p: p.stat().st_mtime) \
        if log_dir.is_dir() else []
    findings = []
    for p in logs:
        try:
            ev = parse_restore_log(p.read_text(errors="replace"))
        except OSError:
            continue
        if ev["space_failure"]:
            findings.append((p, ev))
    return {"logs_checked": len(logs), "findings": findings}


def cmd_diagnose(args) -> int:
    step("Diagnose (Finder restore logs)")
    d = diagnose(Path(args.log_dir))
    info(f"Checked {d['logs_checked']} log file(s) in {args.log_dir}")
    if not d["findings"]:
        warn("No 'Failed preallocation due to space' failure found.")
        info("This tool only fixes that specific failure. If you have not tried")
        info("a Finder update yet, try one first (Finder > iPhone > Update) so")
        info("the logs show what went wrong.")
        return 1
    path, ev = d["findings"][-1]
    ok(f"Space failure found ({len(d['findings'])} log(s)); latest: {path.name}")
    info(f"Cryptex preallocation that failed: {ev['prealloc_bytes'] / 1e6:,.0f} MB")
    if ev["space_needed_mb"]:
        info(f"Device space check said: needed {ev['space_needed_mb']} MB, "
             f"free {ev['free_before_mb']} MB (passed, then ran out)")
    if ev["min_system_partition_mb"]:
        info(f"minSystemPartitionSize in that restore: {ev['min_system_partition_mb']} MB")
    short = estimate_shortfall_mb(ev)
    if short is not None:
        info(f"Estimated shortfall at the cryptex step: ~{short} MB")
        if short >= args.delta:
            warn(f"Shortfall is larger than the default raise ({args.delta} MB);"
                 " consider --delta with a higher value.")
    if ev["splat_failure"]:
        ok("Failure step confirmed: install_splat (0x06A6), error 100")
    return 0


# --------------------------------------------------------------------------
# 2. Setup: build idevicerestore with a fixed libtatsu
# --------------------------------------------------------------------------

def brew_prefix(formula: Optional[str] = None) -> str:
    r = run_cmd(["brew", "--prefix"] + ([formula] if formula else []))
    return r.stdout.strip()


def pkg_config_path() -> str:
    parts = [str(PREFIX / "lib/pkgconfig")]
    for f in ("openssl@3", "curl", "libzip"):
        p = brew_prefix(f)
        if p:
            parts.append(f"{p}/lib/pkgconfig")
    parts.append(f"{brew_prefix() or '/opt/homebrew'}/lib/pkgconfig")
    return ":".join(parts)


def build_autotools(name: str, repo: str, commit: str, env: Dict) -> None:
    src = SRC_DIR / name
    if not src.exists():
        info(f"Cloning {name} ...")
        subprocess.run(["git", "clone", "-q", repo, str(src)], check=True)
    subprocess.run(["git", "-C", str(src), "fetch", "-q", "origin"], check=True)
    subprocess.run(["git", "-C", str(src), "checkout", "-q", commit], check=True)
    log = LOG_DIR / f"build-{name}.log"
    info(f"Building {name} @ {commit} (log: {log}) ...")
    with open(log, "w") as fh:
        subprocess.run(["make", "distclean"], cwd=src, stdout=fh,
                       stderr=subprocess.STDOUT)
        for cmd in (["./autogen.sh", f"--prefix={PREFIX}"],
                    ["make", f"-j{os.cpu_count() or 4}"],
                    ["make", "install"]):
            r = subprocess.run(cmd, cwd=src, env=env, stdout=fh,
                               stderr=subprocess.STDOUT)
            if r.returncode != 0:
                raise SpaceFixError(f"Building {name} failed at "
                                    f"'{' '.join(cmd)}'. See {log}")


def cmd_setup(args) -> int:
    step("Setup")
    if sys.platform != "darwin":
        raise SpaceFixError("spacefix currently supports macOS only.")
    if not shutil.which("brew"):
        raise SpaceFixError("Homebrew is required: https://brew.sh")
    for d in (SRC_DIR, LOG_DIR):
        d.mkdir(parents=True, exist_ok=True)

    # Install only what is missing, and never upgrade anything the user
    # already has (a plain `brew install` upgrades outdated formulae and
    # their dependents, which can touch unrelated software).
    missing = [f for f in BREW_DEPS
               if run_cmd(["brew", "list", "--versions", f]).returncode != 0]
    if missing:
        info(f"Installing missing Homebrew packages: {', '.join(missing)}")
        r = subprocess.run(["brew", "install", *missing], env={
            **os.environ,
            "HOMEBREW_NO_AUTO_UPDATE": "1",
            "HOMEBREW_NO_INSTALL_UPGRADE": "1",
            "HOMEBREW_NO_INSTALLED_DEPENDENTS_CHECK": "1",
        })
        if r.returncode != 0:
            raise SpaceFixError("brew install failed (see output above).")
    else:
        ok("All Homebrew build dependencies already installed.")

    env = {**os.environ, "PKG_CONFIG_PATH": pkg_config_path()}
    build_autotools("libtatsu", LIBTATSU_REPO, LIBTATSU_COMMIT, env)
    tss = (SRC_DIR / "libtatsu/src/tss.c").read_text(errors="replace")
    if '"Yonkers,SysTopPatch"' not in tss:
        raise SpaceFixError("libtatsu source lacks the Yonkers,SysTopPatch fix.")
    build_autotools("idevicerestore", IDEVICERESTORE_REPO,
                    IDEVICERESTORE_COMMIT, env)

    v = run_cmd([str(IDEVICERESTORE), "--version"]).stdout.strip()
    ok(f"Built: {v}")
    ok(f"Installed under {PREFIX}")
    return 0


def check_tools() -> List[str]:
    problems = []
    if sys.platform != "darwin":
        problems.append("macOS is required.")
    if not IDEVICERESTORE.exists():
        problems.append("idevicerestore is not built yet: run `spacefix.py setup`.")
    else:
        v = run_cmd([str(IDEVICERESTORE), "--version"]).stdout
        m = re.search(r"libtatsu ([\w.\-]+)", v)
        if not m or m.group(1) in ("1.0.0", "1.0.1", "1.0.2", "1.0.3",
                                   "1.0.4", "1.0.5"):
            problems.append(f"libtatsu in idevicerestore is too old ({v.strip()});"
                            " run `spacefix.py setup` again.")
    if not shutil.which("irecovery"):
        problems.append("irecovery not found (brew install libirecovery).")
    return problems


def cmd_doctor(args) -> int:
    step("Doctor")
    problems = check_tools()
    for p in problems:
        warn(p)
    if not problems:
        ok(run_cmd([str(IDEVICERESTORE), "--version"]).stdout.strip())
    info(f"Free space in {SPACEFIX_HOME}: {gb(free_bytes(SPACEFIX_HOME))}")
    dev = query_device()
    if dev:
        ok(f"Device: {dev.get('NAME') or dev.get('PRODUCT')} "
           f"({dev.get('PRODUCT')}, {dev.get('MODEL')}) in {dev.get('MODE')} mode")
    else:
        info("No device in Recovery/DFU mode detected.")
    return 1 if problems else 0


# --------------------------------------------------------------------------
# 3. Device
# --------------------------------------------------------------------------

def query_device() -> Optional[Dict[str, str]]:
    """Ask a device in Recovery/DFU mode for its identity via irecovery."""
    if not shutil.which("irecovery"):
        return None
    try:
        r = run_cmd(["irecovery", "-q"], timeout=15)
    except subprocess.TimeoutExpired:
        return None
    if r.returncode != 0:
        return None
    fields = {}
    for line in r.stdout.splitlines():
        if ":" in line:
            k, v = line.split(":", 1)
            fields[k.strip()] = v.strip()
    return fields if fields.get("PRODUCT") else None


def device_on_usb_restore_mode() -> bool:
    """True if a device sits in the restore ramdisk (USB product 0x12AC)."""
    r = run_cmd(["ioreg", "-p", "IOUSB", "-l", "-w0"])
    return '"idProduct" = 4780' in r.stdout


def wait_for_stable_device(seconds: int = 10) -> Dict[str, str]:
    dev = query_device()
    if not dev:
        if device_on_usb_restore_mode():
            raise SpaceFixError(
                "The iPhone is stuck in the restore screen (Apple logo + bar)."
                " Force-restart it into Recovery Mode: Volume Up, Volume Down,"
                " then hold the Side button until the computer + cable screen.")
        raise SpaceFixError(
            "No iPhone in Recovery Mode found. Connect it with a cable directly"
            " to the Mac and make sure it shows the computer + cable screen.")
    if dev.get("MODE", "").lower() not in ("recovery", "dfu"):
        raise SpaceFixError(f"Device mode is {dev.get('MODE')}; it must be "
                            "Recovery Mode.")
    info(f"Checking the USB connection stays up for {seconds}s ...")
    for _ in range(seconds):
        time.sleep(1)
        again = query_device()
        if not again or again.get("ECID") != dev.get("ECID"):
            raise SpaceFixError(
                "The connection dropped during the check. Use another cable or"
                " port, plugged directly into the Mac (no hub), and try again.")
    return dev


# --------------------------------------------------------------------------
# 4. Firmware
# --------------------------------------------------------------------------

def signed_firmwares(product: str) -> List[Dict]:
    url = f"https://api.ipsw.me/v4/device/{product}?type=ipsw"
    with urllib.request.urlopen(url, timeout=30) as r:
        data = json.load(r)
    return [f for f in data.get("firmwares", []) if f.get("signed")]


def ipsw_manifest(ipsw: Path) -> Dict:
    with zipfile.ZipFile(ipsw) as z:
        return plistlib.loads(z.read("BuildManifest.plist"))


def sha1_file(path: Path) -> str:
    h = hashlib.sha1()
    with open(path, "rb") as f:
        for chunk in iter(lambda: f.read(8 << 20), b""):
            h.update(chunk)
    return h.hexdigest()


def download(url: str, dest: Path, size: int) -> None:
    part = dest.with_suffix(dest.suffix + ".part")
    have = part.stat().st_size if part.exists() else 0
    req = urllib.request.Request(url)
    if have:
        req.add_header("Range", f"bytes={have}-")
    with urllib.request.urlopen(req, timeout=60) as r, open(part, "ab") as f:
        if have and r.status != 206:
            f.truncate(0)
            have = 0
        last = 0.0
        while True:
            chunk = r.read(4 << 20)
            if not chunk:
                break
            f.write(chunk)
            have += len(chunk)
            if time.time() - last > 2:
                print(f"\r  downloading {gb(have)} / {gb(size)}", end="",
                      flush=True)
                last = time.time()
    print()
    part.rename(dest)


def get_ipsw(product: str, given: Optional[str], skip_sign_check: bool) -> Path:
    signed = [] if skip_sign_check else signed_firmwares(product)
    if given:
        ipsw = Path(given).expanduser()
        if not ipsw.is_file():
            raise SpaceFixError(f"IPSW not found: {ipsw}")
        man = ipsw_manifest(ipsw)
        if product not in man.get("SupportedProductTypes", []):
            raise SpaceFixError(f"{ipsw.name} is not for {product}.")
        build = man.get("ProductBuildVersion")
        if signed and build not in {f["buildid"] for f in signed}:
            raise SpaceFixError(
                f"Build {build} is not signed by Apple anymore. Signed now: "
                + ", ".join(f"{f['version']} ({f['buildid']})" for f in signed))
        ok(f"Using {ipsw.name} (iOS {man.get('ProductVersion')}, {build})")
        return ipsw

    if not signed:
        raise SpaceFixError(f"No signed firmware listed for {product}.")
    fw = signed[0]
    dest = CACHE_DIR / Path(fw["url"]).name
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if not dest.exists():
        need = fw["filesize"] * 1.05
        if free_bytes(CACHE_DIR) < need:
            raise SpaceFixError(f"Not enough free space to download "
                                f"{gb(fw['filesize'])} into {CACHE_DIR}.")
        info(f"Downloading iOS {fw['version']} ({fw['buildid']}) from Apple ...")
        download(fw["url"], dest, fw["filesize"])
    if fw.get("sha1sum"):
        info("Verifying checksum ...")
        if sha1_file(dest) != fw["sha1sum"]:
            dest.unlink()
            raise SpaceFixError("Checksum mismatch; the download was deleted. "
                                "Run again to re-download.")
    ok(f"Firmware ready: {dest.name}")
    return dest


def extract_ipsw(ipsw: Path, build: str) -> Path:
    out = WORK_DIR / build
    marker = out / ".spacefix-extracted"
    if marker.exists():
        ok(f"Using already extracted firmware in {out}")
        return out
    with zipfile.ZipFile(ipsw) as z:
        need = sum(i.file_size for i in z.infolist())
    if free_bytes(WORK_DIR) < need + 2e9:
        raise SpaceFixError(f"Extracting needs about {gb(need + 2e9)} free in "
                            f"{WORK_DIR}; only {gb(free_bytes(WORK_DIR))} free.")
    if out.exists():
        shutil.rmtree(out)
    out.mkdir(parents=True)
    info(f"Extracting firmware to {out} (a few minutes) ...")
    r = run_cmd(["ditto", "-x", "-k", str(ipsw), str(out)])
    if r.returncode != 0:
        raise SpaceFixError(f"Extraction failed: {r.stderr.strip()}")
    marker.touch()
    return out


# --------------------------------------------------------------------------
# 5. Patch the Upgrade build identity
# --------------------------------------------------------------------------

def find_upgrade_identity(manifest: Dict, model: str) -> Dict:
    """Return the single Update-behaviour identity for this board."""
    model = model.lower()
    matches = [
        bi for bi in manifest.get("BuildIdentities", [])
        if bi.get("Info", {}).get("DeviceClass", "").lower() == model
        and bi["Info"].get("RestoreBehavior") == "Update"
        and "Upgrade" in bi["Info"].get("Variant", "")
    ]
    if len(matches) != 1:
        raise SpaceFixError(
            f"Expected exactly one Upgrade (keep-data) identity for {model}, "
            f"found {len(matches)}. Refusing to continue.")
    if not isinstance(matches[0]["Info"].get("MinimumSystemPartition"), int):
        raise SpaceFixError("Upgrade identity has no MinimumSystemPartition.")
    return matches[0]


def patch_manifest(fw_dir: Path, model: str, delta_mb: int) -> Dict:
    """Raise MinimumSystemPartition in the Upgrade identity only. Always works
    from the pristine backup, so running it twice never stacks the raise."""
    manifest_path = fw_dir / "BuildManifest.plist"
    original = fw_dir / "BuildManifest.original.plist"
    if not original.exists():
        shutil.copy2(manifest_path, original)
    data = plistlib.loads(original.read_bytes())
    before = {id(bi): dict(bi.get("Info", {})) for bi in data["BuildIdentities"]}

    target = find_upgrade_identity(data, model)
    old = target["Info"]["MinimumSystemPartition"]
    target["Info"]["MinimumSystemPartition"] = old + delta_mb

    for bi in data["BuildIdentities"]:
        if bi is not target and bi.get("Info") != before[id(bi)]:
            raise SpaceFixError("Internal error: a non-Upgrade identity changed.")

    os.chmod(manifest_path, 0o644)
    manifest_path.write_bytes(
        plistlib.dumps(data, fmt=plistlib.FMT_XML, sort_keys=False))
    return {"variant": target["Info"]["Variant"], "old": old,
            "new": old + delta_mb}


# --------------------------------------------------------------------------
# 6. Run idevicerestore in update mode, with guards
# --------------------------------------------------------------------------

ERASE_FLAGS = {"-e", "--erase"}


def build_restore_args(variant: str, fw_dir: Path, logfile: Path) -> List[str]:
    args = [str(IDEVICERESTORE), "--variant", variant,
            "--logfile", str(logfile), str(fw_dir)]
    if ERASE_FLAGS & set(args) or "Erase" in variant:
        raise SpaceFixError("Refusing to build an erasing restore command.")
    return args


MILESTONES = [
    ("without erasing user data", "Mode confirmed: update WITHOUT erasing data"),
    ("successfully entered restore mode", "iPhone entered the installer"),
    ("Sending filesystem now", "Space check passed; sending the iOS system"),
    ("Done sending filesystem", "iOS system sent"),
    ("Received Yonkers ticket", "Yonkers firmware signed"),
    ("Done sending Cryptex1,SystemOS", "6 GB cryptex written (install_splat)"),
    ("Status: Restore Finished", "Restore finished"),
]

FAILURE_HINTS = [
    ("Broken pipe", "USB connection dropped. Use another cable/port, directly on"
     " the Mac, keep everything still, put the phone back in Recovery, retry."),
    ("Unable to find device", "The iPhone disappeared from USB. Reconnect it"
     " (another cable/port), put it in Recovery Mode, retry."),
    ("Unable to fetch Yonkers ticket", "Yonkers signing failed: libtatsu is too"
     " old. Run `spacefix.py setup` again."),
    ("Failed preallocation", "Still not enough space at the cryptex step. Retry"
     " with a larger --delta (e.g. --delta 2500)."),
    ("failed to install splat", "Still failing at install_splat. Retry with a"
     " larger --delta (e.g. --delta 2500)."),
]


def keep_updater_closed(stop: threading.Event) -> None:
    """Apple's iPhone Updater pops up for Recovery-mode devices and can grab
    the USB connection mid-transfer. Keep it closed during the restore."""
    while not stop.is_set():
        subprocess.run(["pkill", "-x", UPDATER_APP],
                       stdout=subprocess.DEVNULL, stderr=subprocess.DEVNULL)
        stop.wait(1)


def iter_output(stream) -> Iterator[str]:
    """Yield output lines, treating \\r progress updates as line breaks."""
    buf = ""
    while True:
        ch = stream.read(1)
        if not ch:
            if buf:
                yield buf
            return
        if ch in "\r\n":
            if buf:
                yield buf
            buf = ""
        else:
            buf += ch


def run_restore(cmd: List[str], transcript: Path) -> int:
    stop = threading.Event()
    guard = threading.Thread(target=keep_updater_closed, args=(stop,),
                             daemon=True)
    guard.start()
    proc = subprocess.Popen(cmd, stdin=subprocess.DEVNULL,
                            stdout=subprocess.PIPE, stderr=subprocess.STDOUT,
                            text=True, bufsize=1)
    caffeinate = subprocess.Popen(["caffeinate", "-dimsu", "-w", str(proc.pid)])
    seen_update_mode = False
    hints = []
    last_pct = ""
    try:
        with open(transcript, "w") as log:
            for line in iter_output(proc.stdout):
                log.write(line + "\n")
                if "will erase" in line.lower() or "erasing all data" in line.lower():
                    proc.kill()
                    raise SpaceFixError("idevicerestore announced an ERASE; "
                                        "aborted before anything was sent.")
                if "without erasing user data" in line:
                    seen_update_mode = True
                if not seen_update_mode and line.startswith("Sending "):
                    proc.kill()
                    raise SpaceFixError("Update mode was not confirmed; aborted "
                                        "before the installer was sent.")
                for needle, text in MILESTONES:
                    if needle in line:
                        ok(text)
                for needle, text in FAILURE_HINTS:
                    if needle in line and text not in hints:
                        hints.append(text)
                        warn(line.strip())
                m = re.search(r"(\S+) \[=*\s*\]\s+(\d+)\.\d%", line)
                if m and m.group(2) != last_pct and int(m.group(2)) % 10 == 0:
                    last_pct = m.group(2)
                    info(f"... {m.group(1)} {last_pct}%")
        proc.wait()
    finally:
        stop.set()
        if proc.poll() is None:
            proc.send_signal(signal.SIGINT)
        caffeinate.terminate()

    finished = "Status: Restore Finished" in transcript.read_text(errors="replace")
    if finished:
        return 0
    for h in hints:
        warn(h)
    return 1


def cmd_run(args) -> int:
    print(__doc__.split("\n\n")[0].strip())

    if not args.skip_diagnose:
        if cmd_diagnose(args) != 0:
            raise SpaceFixError("Diagnosis did not match. Use --skip-diagnose "
                                "only if you are sure the error is the same.")

    step("Tools")
    problems = check_tools()
    if problems and not args.dry_run:
        raise SpaceFixError(" ".join(problems))
    for p in problems:
        warn(p)

    step("Device")
    if args.product and args.model:
        dev = {"PRODUCT": args.product, "MODEL": args.model, "MODE": "n/a"}
        warn("Using --product/--model from the command line (no device check).")
    else:
        dev = wait_for_stable_device()
        ok(f"{dev.get('NAME') or dev['PRODUCT']} ({dev['PRODUCT']}, "
           f"{dev['MODEL']}) in Recovery Mode, connection stable")

    step("Firmware")
    if args.extracted:
        fw_dir = Path(args.extracted).expanduser()
        if not (fw_dir / "BuildManifest.plist").is_file():
            raise SpaceFixError(f"No BuildManifest.plist in {fw_dir}")
        ok(f"Using extracted firmware at {fw_dir}")
    else:
        ipsw = get_ipsw(dev["PRODUCT"], args.ipsw, args.skip_sign_check)
        build = ipsw_manifest(ipsw).get("ProductBuildVersion", "unknown")
        fw_dir = extract_ipsw(ipsw, build)

    step("Patch (Upgrade identity only)")
    patch = patch_manifest(fw_dir, dev["MODEL"], args.delta)
    ok(f"{patch['variant']}: MinimumSystemPartition "
       f"{patch['old']} -> {patch['new']} MB (+{args.delta})")

    LOG_DIR.mkdir(parents=True, exist_ok=True)
    stamp = time.strftime("%Y%m%d-%H%M%S")
    logfile = LOG_DIR / f"idevicerestore-{stamp}.log"
    transcript = LOG_DIR / f"run-{stamp}.txt"
    cmd = build_restore_args(patch["variant"], fw_dir, logfile)

    step("Ready")
    info("Command: " + " ".join(f"'{c}'" if " " in c else c for c in cmd))
    if args.dry_run:
        ok("Dry run: stopping here. Nothing was sent to the iPhone.")
        return 0

    print("""
  This is an UNOFFICIAL method. It runs an update (not a restore), which is
  designed to keep your data, but nothing is guaranteed. Do not touch the
  phone, cable or Mac until it finishes (20-40 min). If Apple's iPhone
  Updater window appears, it will be closed automatically.
""")
    if not args.yes:
        typed = input(f'  Type "{CONFIRM_PHRASE}" to start: ').strip()
        if typed != CONFIRM_PHRASE:
            info("Cancelled. Nothing was sent to the iPhone.")
            return 1

    step("Updating (keep data)")
    rc = run_restore(cmd, transcript)
    info(f"Full log: {logfile}")
    if rc == 0:
        print("""
  Done. The iPhone will restart, may show a progress bar or "Attempting Data
  Recovery" - leave it plugged in until it finishes. Then:
    1. Unlock it and check your photos/messages.
    2. Back up immediately (Finder or iCloud).
    3. Free at least 10 GB so this cannot happen again.
""")
    else:
        warn("The update did not finish. Your data was not erased by this tool;"
             " the phone should be back in (or need a force-restart to) "
             "Recovery Mode. See the hints above.")
    return rc


# --------------------------------------------------------------------------

def main(argv: Optional[List[str]] = None) -> int:
    p = argparse.ArgumentParser(
        prog="spacefix.py",
        description="Finish a failed iPhone update (error 100 / install_splat, "
                    "out of storage) without erasing data.")
    p.add_argument("--version", action="version", version=VERSION)
    sub = p.add_subparsers(dest="command", required=True)

    def common(sp):
        sp.add_argument("--log-dir", default=str(FINDER_LOG_DIR),
                        help="Finder restore logs folder")
        sp.add_argument("--delta", type=int, default=DEFAULT_DELTA_MB,
                        help=f"MB added to MinimumSystemPartition "
                             f"(default {DEFAULT_DELTA_MB})")

    sp = sub.add_parser("diagnose", help="check Finder logs for the space failure")
    common(sp)
    sp.set_defaults(func=cmd_diagnose)

    sp = sub.add_parser("setup", help="build idevicerestore with fixed libtatsu")
    sp.set_defaults(func=cmd_setup)

    sp = sub.add_parser("doctor", help="check tools, space and connected device")
    sp.set_defaults(func=cmd_doctor)

    sp = sub.add_parser("run", help="patch firmware and run the keep-data update")
    common(sp)
    sp.add_argument("--ipsw", help="use this IPSW instead of downloading")
    sp.add_argument("--extracted", help="use an already extracted IPSW folder")
    sp.add_argument("--dry-run", action="store_true",
                    help="do everything except sending anything to the iPhone")
    sp.add_argument("--skip-diagnose", action="store_true")
    sp.add_argument("--skip-sign-check", action="store_true",
                    help="do not check Apple's signing status online")
    sp.add_argument("--product", help="(dry run) product type, e.g. iPhone16,2")
    sp.add_argument("--model", help="(dry run) board, e.g. d84ap")
    sp.add_argument("--yes", action="store_true",
                    help="skip the typed confirmation")
    sp.set_defaults(func=cmd_run)

    args = p.parse_args(argv)
    if args.command == "run" and bool(args.product) != bool(args.model):
        p.error("--product and --model must be given together")
    if args.command == "run" and args.product and not args.dry_run:
        p.error("--product/--model are only allowed with --dry-run")
    try:
        return args.func(args)
    except SpaceFixError as e:
        print(f"\n  [x] {e}", file=sys.stderr)
        return 2
    except KeyboardInterrupt:
        print("\n  Interrupted.", file=sys.stderr)
        return 130


if __name__ == "__main__":
    sys.exit(main())
