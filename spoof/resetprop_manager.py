"""
Standalone resetprop manager (no Magisk, no su binary required).

Downloads, caches, pushes and invokes ``Enginex0/resetprop-rs`` — a small
static Rust binary (~400 KB, zero Magisk/bionic dependencies) that edits
Android's ``/dev/__properties__`` shared-memory property areas directly via
mmap under ``adb root`` (``uid=0``).

Why this exists:
- On Android 10-15 ``google_apis`` AVDs, ``setprop ro.*`` is ignored by
  ``init``, ``/data/local.prop`` is ignored, and ``/system`` / ``/product`` /
  ``/vendor`` are read-only unless the emulator was launched with
  ``-writable-system`` AND AVB/dm-verity was disabled AND rebooted.
- Even with writable ``build.prop``, kernel-cmdline / ``qemu-props`` values
  (``ro.serialno``, ``ro.boot.serialno``, ``ro.kernel.qemu``, ``ro.hardware``,
  ``ro.baseband``, ``ro.bootloader``) cannot be changed via ``build.prop``.
- ``resetprop-rs --stealth`` mutates every ``ro.*`` property in memory in
  under a second (updating Android 14 ``appcompat_override`` areas as well),
  without touching ``/system`` or rebooting the emulator.
"""

from __future__ import annotations

import os
import struct
import subprocess
import tempfile
import time
import urllib.error
import urllib.request
from pathlib import Path

import config
from core import adb as adb_helper

RESETPROP_VERSION = "v0.6.0"
RESETPROP_RELEASE_URL = (
    "https://github.com/Enginex0/resetprop-rs/releases/download/"
    "{version}/resetprop-{abi}"
)
REMOTE_PATH = "/data/local/tmp/resetprop-rs"
REMOTE_SCRIPT_PATH = "/data/local/tmp/.ad_rp.sh"

# ELF e_machine values for each Android ABI
_ELF_MACHINES = {
    "x86_64": 62,       # EM_X86_64
    "x86": 3,           # EM_386
    "arm64-v8a": 183,   # EM_AARCH64
    "armeabi-v7a": 40,  # EM_ARM
}
_MIN_BINARY_BYTES = 50_000  # Real v0.6.0 static binaries are ~320-510 KB
_DOWNLOAD_RETRIES = 2
_DOWNLOAD_TIMEOUT = 15


def normalize_abi(raw: str | None) -> str | None:
    """Map ``ro.product.cpu.abi`` to a resetprop-rs release asset ABI."""
    abi = (raw or "").strip().lower()
    if not abi:
        return None
    if abi in ("x86_64", "amd64"):
        return "x86_64"
    if abi in ("x86", "i386", "i686"):
        return "x86"
    if abi in ("arm64-v8a", "arm64", "aarch64"):
        return "arm64-v8a"
    if abi in ("armeabi-v7a", "armeabi", "arm"):
        return "armeabi-v7a"
    return None


def cache_path(abi: str) -> Path:
    return Path(config.DATA_DIR) / f"resetprop-{abi}"


def version_path(abi: str) -> Path:
    return Path(config.DATA_DIR) / f"resetprop-{abi}.version"


def verify_local_binary(path: Path, abi: str | None = None) -> tuple[bool, str]:
    """Validate that *path* is an ELF executable matching *abi*."""
    try:
        if not path.is_file():
            return False, f"file does not exist: {path}"
        size = path.stat().st_size
        if size < _MIN_BINARY_BYTES:
            return False, f"file too small ({size} bytes; expected >= {_MIN_BINARY_BYTES})"
        with open(path, "rb") as fh:
            header = fh.read(20)
        if len(header) < 20 or header[:4] != b"\x7fELF":
            return False, "not an ELF binary"
        # Byte 5 is EI_DATA (1 = little-endian, 2 = big-endian)
        endian = "<" if header[5] != 2 else ">"
        e_machine = struct.unpack(f"{endian}H", header[18:20])[0]
        if abi:
            expected = _ELF_MACHINES.get(abi)
            if expected is not None and e_machine != expected:
                return (
                    False,
                    f"ELF architecture mismatch: e_machine={e_machine}, expected {expected} ({abi})",
                )
        return True, f"valid ELF ({size} bytes)"
    except OSError as exc:
        return False, f"could not read binary: {exc}"


def _sh_quote(text: str) -> str:
    """POSIX single-quote escaping safe for any property key or value."""
    return "'" + str(text).replace("'", "'\"'\"'") + "'"


class ResetpropManager:
    """Downloads, deploys, and runs ``resetprop-rs`` on a rooted emulator."""

    def __init__(self, device_serial: str):
        self.device_serial = device_serial

    # ------------------------------------------------------------------ adb IO
    def _adb(self, args: list[str], timeout: int = 30, retry: bool = True) -> tuple[bool, str, str]:
        cmd = [config.ADB_PATH, "-s", self.device_serial, *args]
        try:
            proc = subprocess.run(cmd, capture_output=True, text=True, timeout=timeout)
        except subprocess.TimeoutExpired:
            return False, "", f"adb command timed out after {timeout}s"
        except FileNotFoundError:
            return False, "", f"adb not found at {config.ADB_PATH}"
        except Exception as exc:
            return False, "", str(exc)

        out = (proc.stdout or "").strip()
        err = (proc.stderr or "").strip()
        if proc.returncode != 0 and retry:
            combined = f"{out} {err}".lower()
            if (
                "offline" in combined
                or "no devices" in combined
                or ("device '" in combined and "not found" in combined)
                or "closed" in combined
                or "broken pipe" in combined
            ):
                ok, _ = adb_helper.recover(self.device_serial, timeout=20)
                if ok:
                    return self._adb(args, timeout=timeout, retry=False)
        return proc.returncode == 0, out, err

    def _shell(self, cmd: str, timeout: int = 30) -> tuple[bool, str, str]:
        return self._adb(["shell", cmd], timeout=timeout)

    def _push_file(self, local_path: Path, remote_path: str, timeout: int = 60) -> tuple[bool, str]:
        ok, out, err = self._adb(["push", str(local_path), remote_path], timeout=timeout)
        if ok:
            return True, out or "pushed"

        # Fallback: stream bytes via stdin if `adb push` hits a Windows path quirk
        try:
            data = local_path.read_bytes()
            proc = subprocess.run(
                [config.ADB_PATH, "-s", self.device_serial, "shell", f"cat > {remote_path}"],
                input=data,
                capture_output=True,
                timeout=timeout,
            )
            if proc.returncode == 0:
                return True, "streamed via stdin"
            stderr_text = (proc.stderr or b"").decode("utf-8", errors="replace").strip()
            return False, stderr_text or err or out or "push failed"
        except Exception as exc:
            return False, err or str(exc)

    # --------------------------------------------------------------- discovery
    def detect_device_abi(self) -> str | None:
        ok, out, _ = self._shell("getprop ro.product.cpu.abi", timeout=15)
        if not ok or not out:
            return None
        return normalize_abi(out.splitlines()[0].strip())

    def detect_device_binary(self) -> tuple[str | None, str | None]:
        """Return ``(remote_path, kind)`` if a usable resetprop is already on device."""
        ok, out, err = self._shell(f"{REMOTE_PATH} --help 2>&1", timeout=15)
        combined = f"{out}\n{err}"
        if ok and ("--stealth" in combined or "resetprop" in combined.lower()):
            kind = "resetprop-rs" if "--stealth" in combined else "magisk"
            return REMOTE_PATH, kind

        ok, which_out, _ = self._shell("which resetprop 2>/dev/null", timeout=15)
        candidate = (which_out.splitlines()[0].strip() if ok and which_out else "")
        if candidate and candidate.startswith("/"):
            ok2, h_out, h_err = self._shell(f"{candidate} --help 2>&1", timeout=15)
            h_combined = f"{h_out}\n{h_err}".lower()
            if ok2 or "usage" in h_combined or "resetprop" in h_combined:
                kind = "resetprop-rs" if "--stealth" in h_combined else "magisk"
                return candidate, kind

        return None, None

    # --------------------------------------------------------------- download
    @staticmethod
    def download_binary(abi: str, log=None) -> tuple[bool, Path | None, str]:
        """Ensure ``data/resetprop-<abi>`` exists locally and is a valid ELF."""
        abi = normalize_abi(abi) or ""
        if abi not in _ELF_MACHINES:
            return False, None, f"unsupported ABI: {abi!r}"

        target = cache_path(abi)
        ver_file = version_path(abi)
        target.parent.mkdir(parents=True, exist_ok=True)

        # Reuse valid cached binary (if a version marker exists, require it to match
        # unless no marker exists, which lets users drop a local binary manually).
        valid, _ = verify_local_binary(target, abi=abi)
        if valid:
            cached_ver = ""
            try:
                cached_ver = ver_file.read_text(encoding="utf-8").strip()
            except OSError:
                pass
            if not cached_ver or cached_ver == RESETPROP_VERSION:
                return True, target, f"cached ({target})"

        url = RESETPROP_RELEASE_URL.format(version=RESETPROP_VERSION, abi=abi)
        part = target.with_suffix(".part")
        last_err = "download failed"

        for attempt in range(1, _DOWNLOAD_RETRIES + 1):
            try:
                if log:
                    log(f"Downloading resetprop-rs {RESETPROP_VERSION} ({abi})...")
                req = urllib.request.Request(
                    url,
                    headers={"User-Agent": "AntiDetect-Android-Emulator/resetprop-manager"},
                )
                with urllib.request.urlopen(req, timeout=_DOWNLOAD_TIMEOUT) as resp:
                    with open(part, "wb") as out_fh:
                        while True:
                            chunk = resp.read(65536)
                            if not chunk:
                                break
                            out_fh.write(chunk)

                valid, reason = verify_local_binary(part, abi=abi)
                if not valid:
                    part.unlink(missing_ok=True)
                    last_err = f"downloaded binary failed validation: {reason}"
                    continue

                os.replace(part, target)
                try:
                    ver_file.write_text(RESETPROP_VERSION + "\n", encoding="utf-8")
                except OSError:
                    pass
                return True, target, f"downloaded {RESETPROP_VERSION} ({abi})"
            except (urllib.error.URLError, OSError, TimeoutError) as exc:
                part.unlink(missing_ok=True)
                last_err = f"download failed ({exc})"
                # Fail fast on network/TLS/DNS errors so offline machines fall back quickly
                if attempt < _DOWNLOAD_RETRIES:
                    time.sleep(0.4)

        return False, None, last_err

    def ensure_on_device(self, log=None) -> tuple[bool, str | None, str]:
        """Guarantee a working resetprop binary is present on the emulator.

        Returns ``(ok, remote_path, kind_or_error)``.
        """
        existing_path, existing_kind = self.detect_device_binary()
        if existing_path and existing_kind:
            return True, existing_path, existing_kind

        abi = self.detect_device_abi()
        if not abi:
            return False, None, "could not detect device ABI"

        ok, local_bin, detail = self.download_binary(abi, log=log)
        if not ok or not local_bin:
            return False, None, detail

        if log:
            log(f"Pushing resetprop-rs ({abi}) to {REMOTE_PATH}...")
        pushed, push_detail = self._push_file(local_bin, REMOTE_PATH, timeout=60)
        if not pushed:
            return False, None, f"failed to push resetprop-rs: {push_detail}"

        self._shell(f"chmod 755 {REMOTE_PATH}", timeout=15)
        ok_help, out, err = self._shell(f"{REMOTE_PATH} --help 2>&1", timeout=15)
        combined = f"{out}\n{err}"
        if not ok_help and "resetprop" not in combined.lower() and "--stealth" not in combined:
            return False, None, f"pushed resetprop-rs did not execute: {combined[:200]}"

        kind = "resetprop-rs" if "--stealth" in combined else "magisk"
        return True, REMOTE_PATH, kind

    # --------------------------------------------------------------- execution
    def apply_props(self, props: dict[str, str], log=None) -> tuple[bool, str, str | None]:
        """Batch-apply *props* via resetprop in a single ADB round-trip.

        Returns ``(ok, detail, binary_kind)``.
        """
        cleaned = {
            str(k).strip(): str(v).replace("\r", "").replace("\n", " ").strip()
            for k, v in (props or {}).items()
            if str(k).strip() and v is not None and str(v).strip() != ""
        }
        if not cleaned:
            return True, "no properties to apply", None

        # Ensure adbd is uid=0 (waits cleanly for adbd restart if needed)
        ok_id, id_out, _ = self._shell("id", timeout=15)
        if not (ok_id and "uid=0" in id_out):
            rooted, root_detail = adb_helper.root(self.device_serial, timeout=60)
            if not rooted:
                return False, f"resetprop requires root: {root_detail}", None

        ok_bin, remote_path, kind_or_err = self.ensure_on_device(log=log)
        if not ok_bin or not remote_path:
            return False, kind_or_err, None
        kind = kind_or_err

        # Build a single POSIX sh script so all props apply in one ADB call
        # (~0.2s) and a failure on one optional partition prop never aborts
        # the rest (unlike `resetprop-rs -f`, which stops on first error).
        lines = ["#!/system/bin/sh", "FAIL=0"]
        for key, value in cleaned.items():
            qk = _sh_quote(key)
            qv = _sh_quote(value)
            is_long = len(value.encode("utf-8")) >= 92
            if kind == "resetprop-rs":
                if is_long:
                    # Converting an existing short prop (<92B) to a long prop
                    # (>=92B) in resetprop-rs requires deleting the short slot
                    # first so alloc_long_prop_info() allocates a long buffer.
                    lines.append(
                        f"{remote_path} --stealth {qk} {qv} >/dev/null 2>&1 "
                        f"|| ({remote_path} -d {qk} >/dev/null 2>&1 && {remote_path} --stealth {qk} {qv} >/dev/null) "
                        f"|| {remote_path} -n {qk} {qv} >/dev/null "
                        f"|| FAIL=$((FAIL+1))"
                    )
                else:
                    lines.append(
                        f"{remote_path} --stealth {qk} {qv} >/dev/null 2>&1 "
                        f"|| {remote_path} -n {qk} {qv} >/dev/null "
                        f"|| FAIL=$((FAIL+1))"
                    )
            else:
                lines.append(
                    f"{remote_path} -n {qk} {qv} >/dev/null || FAIL=$((FAIL+1))"
                )
        lines.append('echo "RESETPROP_DONE fail=$FAIL"')
        script_text = "\n".join(lines) + "\n"

        tmp_path: Path | None = None
        try:
            with tempfile.NamedTemporaryFile(
                mode="w",
                encoding="utf-8",
                newline="\n",
                prefix="ad_rp_",
                suffix=".sh",
                delete=False,
            ) as fh:
                fh.write(script_text)
                tmp_path = Path(fh.name)

            pushed, push_err = self._push_file(tmp_path, REMOTE_SCRIPT_PATH, timeout=30)
            if not pushed:
                return False, f"failed to push resetprop batch script: {push_err}", kind

            self._shell(f"chmod 700 {REMOTE_SCRIPT_PATH}", timeout=15)
            ok_run, out, err = self._shell(f"sh {REMOTE_SCRIPT_PATH} 2>&1", timeout=60)
            combined = f"{out}\n{err}".strip()

            # SELinux safety net: if a strict policy denies /dev/__properties__
            # writes from adb root, momentarily switch permissive, re-run, and
            # ALWAYS restore enforcing.
            if (not ok_run or "fail=0" not in combined) and (
                "permission denied" in combined.lower() or "fail=" in combined
            ):
                _, enf_out, _ = self._shell("getenforce 2>/dev/null", timeout=10)
                if enf_out.strip().lower() == "enforcing" and "fail=0" not in combined:
                    # Only toggle SELinux if the first pass actually reported failures
                    fail_count = _parse_fail_count(combined)
                    if fail_count is None or fail_count > 0:
                        try:
                            ok_se, _, _ = self._shell("setenforce 0 2>/dev/null", timeout=10)
                            if ok_se:
                                ok_run, out, err = self._shell(
                                    f"sh {REMOTE_SCRIPT_PATH} 2>&1", timeout=60
                                )
                                combined = f"{out}\n{err}".strip()
                        finally:
                            self._shell("setenforce 1 2>/dev/null", timeout=10)

            if "RESETPROP_DONE" not in combined:
                return False, f"resetprop batch did not finish: {combined[:240]}", kind

            fail_count = _parse_fail_count(combined) or 0
            applied_count = max(0, len(cleaned) - fail_count)
            if applied_count == 0:
                return False, f"resetprop could not update properties ({combined[:200]})", kind
            return True, f"applied {applied_count}/{len(cleaned)} props via {kind}", kind
        finally:
            self._shell(f"rm -f {REMOTE_SCRIPT_PATH}", timeout=10)
            if tmp_path is not None:
                try:
                    tmp_path.unlink(missing_ok=True)
                except OSError:
                    pass


def _parse_fail_count(output: str) -> int | None:
    for token in (output or "").split():
        if token.startswith("fail="):
            try:
                return int(token.split("=", 1)[1])
            except ValueError:
                return None
    return None
