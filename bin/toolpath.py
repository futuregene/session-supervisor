#!/usr/bin/env python3
"""Find the local toolchain without hardcoding one machine's paths.

Every script here used to spell `/opt/homebrew/bin/future` and
`/opt/homebrew/bin/python3` itself. Two problems with that:

  * the repo stopped working anywhere Homebrew lives elsewhere, or where the
    binary is simply on PATH (a Linux box, an Intel Mac, a devcontainer);
  * it wrote one user's home directory into every file, and with it into the
    history — a machine-specific detail that has no business being there.

Discovery order, per tool:

  1. an explicit path from config (`future_bin`) — the instance decides;
  2. `$FUTURE_BIN`, when set;
  3. the usual install prefixes (`/opt/homebrew/bin`, `/usr/local/bin`,
     `~/.local/bin`) — the *installed* CLI;
  4. whatever `PATH` resolves — last on purpose: a dev shell's PATH often carries
     a working-tree build (`future-os/target/release/future`), and the supervisor
     must drive the CLI the user actually installed, not whatever a build left
     lying in front of it.

`future`/`python3` as a *bare name* means "discover": that is the portable
spelling, and what `bin/config.json` ships with.

2026-10-18: the shift here is the one the user asked for — mechanism stays in
git, this machine's facts do not. Anything that must differ per host belongs in
`bin/config.json` (the one tracked instance file) or an environment variable, not
in a script.
"""
from __future__ import annotations

import os
import shutil
import sys


def _bin_dirs() -> list[str]:
    """Directories worth searching, most specific first, existing ones only."""
    dirs: list[str] = []
    explicit = os.environ.get("FUTURE_TOOLBIN")
    if explicit:
        dirs.append(explicit)
    dirs += ["/opt/homebrew/bin", "/usr/local/bin", os.path.expanduser("~/.local/bin")]
    return [d for d in dirs if d and os.path.isdir(d)]


def utf8_stdout() -> None:
    """Make this process print UTF-8 whatever the host's locale says.

    Windows: when stdout is a pipe, Python encodes with the ANSI code page (cp936
    here, measured 2026-10-20), not UTF-8. Everything downstream assumes UTF-8:
    `bin/wake.sh` captures the text with `$(...)` and hands it to the CLI as an
    argument, where Git Bash's msys runtime decodes those bytes as UTF-8 — so a
    GBK-encoded card arrives as mojibake (the user saw
    `# 一次唤醒回合` become `# һ´λ½ÐѻغÏ`, 2026-10-20), and any script that prints
    Chinese and is captured by a tool crashes outright with UnicodeEncodeError.

    Setting PYTHONIOENCODING in one launcher is not enough: the scripts also run
    from a shell, a scheduled task and a self-test, each with a different
    environment. Reconfiguring at the top of `main` makes the guarantee local to
    the process that prints.

    stdout is strict (UTF-8 encodes every code point, so a failure is a real
    bug), stderr escapes instead of failing — a diagnostic must never be the
    thing that turns into a traceback.
    """
    for stream, errors in ((sys.stdout, "strict"), (sys.stderr, "backslashreplace")):
        try:
            # newline="\n": the text is an argument, not a file; a CR from the
            # Windows text layer has no business inside the card.
            stream.reconfigure(encoding="utf-8", errors=errors, newline="\n")  # type: ignore[union-attr]
        except (AttributeError, ValueError, OSError):
            pass  # replaced stream (StringIO in tests), closed pipe: nothing to do


def future_exe(preferred: str | None = None) -> str:
    """Absolute path to the `future` CLI, or the bare name if nothing is found.

    `preferred` is config's `future_bin`. A real path in there wins outright:
    when the instance names a binary, that is its decision and not a guess. The
    bare name `future` (or an empty value) means "discover".
    """
    cand = (preferred or "").strip()
    if cand and cand != "future" and os.path.exists(cand):
        return cand
    env = os.environ.get("FUTURE_BIN")
    if env and os.path.exists(env):
        return env
    for d in _bin_dirs():
        p = os.path.join(d, "future")
        if os.path.exists(p):
            return p
    found = shutil.which("future")
    if found:
        return found
    return "future"


def python_exe() -> str:
    """The interpreter running this process — always right, never machine-specific."""
    return sys.executable or "python3"


def bash_exe() -> str | None:
    """Absolute path to a bash that can run `bin/*.sh`, or None.

    `bin/watchd.sh` needs a real bash (`command -v`, `kill -0`, `mkdir -p`,
    `date`), and on Windows that is Git Bash, which is **not on PATH** by
    default — `C:\\Program Files\\Git\\bin` is a machine or user PATH entry the
    installer may or may not have added, and a process started before that edit
    never sees it. Resolving it the same way as `future`/`python3` means a
    watcher started by the app inherits a working bash without a reboot.

    `$BASH` wins (a shell that already knows where bash is can say so), then
    PATH, then the Windows install prefixes, then `/bin/bash`.
    """
    explicit = (os.environ.get("BASH") or "").strip()
    if explicit and os.path.exists(explicit):
        return explicit
    found = shutil.which("bash")
    if found:
        return found
    for p in (
        r"C:\Program Files\Git\bin\bash.exe",
        r"C:\Program Files (x86)\Git\bin\bash.exe",
        os.path.expanduser(r"~\AppData\Local\Programs\Git\bin\bash.exe"),
        "/bin/bash",
        "/usr/bin/bash",
    ):
        if os.path.exists(p):
            return p
    return None


def tool_path(base: str | None = None) -> str:
    """A PATH value with the toolchain's own directories in front.

    Keeps whatever PATH already had, so a user's custom install still wins for
    anything else. `[base]` lets a caller substitute a different tail.
    """
    tail = base if base is not None else os.environ.get("PATH", "")
    seen: list[str] = []
    for entry in _bin_dirs() + ([tail] if tail else []):
        for part in entry.split(os.pathsep):
            if part and part not in seen:
                seen.append(part)
    return os.pathsep.join(seen)


def config_path() -> str:
    """Absolute path of the run configuration.

    It lives beside the scripts that read it (`bin/config.json`) because those
    scripts are its only consumers — nothing outside `bin/` opens the file, and
    keeping it at the repository root made the root look like it held a second
    configuration surface next to `AGENTS.md` / `POLICY.md`.

    Spelled once, here, so a future move is one edit rather than seven that can
    disagree about where the file went.
    """
    return os.path.join(os.path.dirname(os.path.abspath(__file__)), "config.json")


if __name__ == "__main__":  # cheap way to see what this host resolves to
    print(f"future  : {future_exe(os.environ.get('FUTURE_BIN') or None)}")
    print(f"python  : {python_exe()}")
    print(f"bash    : {bash_exe()}")
    print(f"toolpath: {tool_path()}")
