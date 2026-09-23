"""Process sandbox for execute_program scripts (macOS Seatbelt via sandbox-exec).

The profile denies everything by default and allows only what a stdlib-only
Python interpreter needs: reading its own install, system libraries, and a
per-run scratch directory (read/write). There is no network, no process
creation, and no access to the rest of the filesystem. Tool calls do not need
any of these: they travel over the child's stdin/stdout to the parent.

`sandbox-exec` is deprecated by Apple but still ships and still enforces;
Claude Code and Codex CLI use it the same way. Keep this module the only place
that knows about it, so another backend can replace it.
"""

from __future__ import annotations

import os
import shutil
import sys

SANDBOX_EXEC = "/usr/bin/sandbox-exec"

SANDBOX_MODES = ("seatbelt", "none")

# Parameters are passed with -D and must be real (symlink-resolved) paths:
# Seatbelt matches the resolved path, so /var/... must be /private/var/...
SEATBELT_PROFILE = """\
(version 1)
(deny default)

; the interpreter itself, and nothing else, may be executed
(allow process-exec (literal (param "PYTHON_EXE")))

; interpreter install (stdlib) + system libraries and the dyld shared cache
(allow file-read* (subpath (param "PYTHON_PREFIX")))
(allow file-read*
  (literal "/")
  (subpath "/usr/lib")
  (subpath "/System/Library")
  (subpath "/System/Volumes/Preboot/Cryptexes")
  (subpath "/private/var/db/dyld")
  (subpath "/usr/share/zoneinfo")
  (subpath "/private/var/db/timezone")
  (literal "/dev/null")
  (literal "/dev/urandom")
  (literal "/dev/random"))
; metadata (not contents) of the public system directories that symlinks
; like /usr/share/zoneinfo -> /var/db/timezone/... resolve through
(allow file-read-metadata
  (literal "/var")
  (literal "/etc")
  (literal "/tmp")
  (literal "/usr")
  (literal "/usr/share")
  (literal "/private")
  (literal "/private/var")
  (literal "/private/var/db"))
; no blanket file-read-metadata: a script cannot even learn whether a file
; exists (or its size/mtime) anywhere else

; per-run scratch directory: the only writable location
(allow file-read* file-write* (subpath (param "SCRATCH")))
(allow file-write-data (literal "/dev/null"))

(allow sysctl-read)
(allow signal (target self))
"""


def seatbelt_available() -> bool:
    return sys.platform == "darwin" and os.access(SANDBOX_EXEC, os.X_OK)


def interpreter() -> tuple[str, str]:
    """Real path of the base interpreter and its install prefix.

    Scripts run on the base interpreter with ``-I -S`` (stdlib only): the
    venv's site-packages, and therefore this server's own dependencies, are not
    importable inside the sandbox.
    """
    exe = os.path.realpath(sys.executable)
    prefix = os.path.realpath(sys.base_prefix)
    if not exe.startswith(prefix + os.sep):
        # e.g. a framework build whose binary lives outside base_prefix
        prefix = os.path.dirname(os.path.dirname(exe))
    return exe, prefix


def build_command(mode: str, runner_source: str, scratch: str) -> list[str]:
    """argv that runs ``runner_source`` in a child interpreter under ``mode``."""
    exe, prefix = interpreter()
    python = [exe, "-I", "-S", "-B", "-c", runner_source]
    if mode == "none":
        return python
    if mode != "seatbelt":
        raise ValueError(f"unknown sandbox mode {mode!r} (expected one of {SANDBOX_MODES})")
    if not seatbelt_available():
        raise RuntimeError(
            "sandbox 'seatbelt' requested but sandbox-exec is not available on this "
            "system; set execution.sandbox to 'none' to run scripts unsandboxed"
        )
    return [
        SANDBOX_EXEC,
        "-p", SEATBELT_PROFILE,
        "-D", f"PYTHON_EXE={exe}",
        "-D", f"PYTHON_PREFIX={prefix}",
        "-D", f"SCRATCH={os.path.realpath(scratch)}",
        *python,
    ]


def child_env(scratch: str) -> dict[str, str]:
    """Minimal environment: nothing from the server's environment is inherited."""
    return {
        "HOME": scratch,
        "TMPDIR": scratch,
        "PATH": "/usr/bin:/bin",
        "LANG": "en_US.UTF-8",
    }


def remove_scratch(path: str) -> None:
    shutil.rmtree(path, ignore_errors=True)
