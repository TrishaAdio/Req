#!/usr/bin/env python3
"""One command to install and run the bot.

    python3 setup.py              build .venv, install requirements, start the bot
    python3 setup.py --install    set up only, don't start
    python3 setup.py --update     reinstall requirements, then start
    python3 setup.py --recreate   throw the venv away and rebuild it

Runs on the system Python with nothing installed, so it imports only the
standard library. Requirements are installed once and re-checked against a
hash of requirements.txt, so a normal start is instant.

systemd should call the venv directly (see deploy/req.service); this script is
for setting the machine up and for running the bot by hand.
"""
from __future__ import annotations

import hashlib
import os
import shutil
import subprocess
import sys
from pathlib import Path
from typing import NoReturn

MIN_PYTHON = (3, 9)
ROOT = Path(__file__).resolve().parent
VENV = ROOT / ".venv"
REQUIREMENTS = ROOT / "requirements.txt"
STAMP = VENV / ".requirements.sha256"
ENV_FILE = ROOT / ".env"
ENV_EXAMPLE = ROOT / ".env.example"
ENTRY = ROOT / "bot.py"

# Values the bot cannot start without, in the order we ask for them.
NEEDED = (
    ("API_ID", "API_ID from my.telegram.org"),
    ("API_HASH", "API_HASH from my.telegram.org"),
    ("BOT_TOKEN", "Bot token from @BotFather"),
    ("OWNER_ID", "Your numeric user id (leave empty to find it from the log)"),
)
PLACEHOLDERS = ("1234567", "your_api_hash_here", "123456:ABC-DEF_your_bot_token",
                "123456789")

_COLOUR = sys.stdout.isatty() and not os.getenv("NO_COLOR")


def paint(text: str, code: str) -> str:
    return f"\033[{code}m{text}\033[0m" if _COLOUR else text


def step(text: str) -> None:
    print(paint("==>", "36;1"), text, flush=True)


def warn(text: str) -> None:
    print(paint("!!!", "33;1"), text, flush=True)


def die(text: str, *hints: str) -> NoReturn:
    print(paint("xxx", "31;1"), text, file=sys.stderr, flush=True)
    for hint in hints:
        print("   ", hint, file=sys.stderr)
    raise SystemExit(1)


def venv_python() -> Path:
    if os.name == "nt":
        return VENV / "Scripts" / "python.exe"
    return VENV / "bin" / "python"


def run(command: list[str], what: str) -> None:
    result = subprocess.run(command, cwd=ROOT)
    if result.returncode != 0:
        die(f"{what} failed (exit {result.returncode})")


# --- steps ----------------------------------------------------------------
def check_python() -> None:
    if sys.version_info < MIN_PYTHON:
        die(
            "Python {}.{}+ is required, this is {}.{}".format(
                *MIN_PYTHON, *sys.version_info[:2]),
            "Install a newer Python and run this again with it.",
        )


def build_venv(recreate: bool = False) -> None:
    if recreate and VENV.exists():
        step("removing the old .venv")
        shutil.rmtree(VENV)
    if venv_python().exists():
        return
    step("creating .venv")
    result = subprocess.run([sys.executable, "-m", "venv", str(VENV)], cwd=ROOT)
    if result.returncode != 0 or not venv_python().exists():
        die(
            "could not create the virtual environment",
            "On Debian/Ubuntu: sudo apt install python3-venv",
            "On Fedora/RHEL:   sudo dnf install python3-virtualenv",
        )


def requirements_hash() -> str:
    return hashlib.sha256(REQUIREMENTS.read_bytes()).hexdigest()


def install(update: bool = False) -> None:
    if not REQUIREMENTS.exists():
        die(f"{REQUIREMENTS.name} is missing")
    wanted = requirements_hash()
    if not update and STAMP.exists() and STAMP.read_text().strip() == wanted:
        step("requirements already installed")
        return

    python = str(venv_python())
    step("installing requirements")
    subprocess.run([python, "-m", "pip", "install", "--quiet",
                    "--upgrade", "pip"], cwd=ROOT)
    run([python, "-m", "pip", "install", "--quiet", "--upgrade",
         "-r", str(REQUIREMENTS)], "pip install")
    STAMP.write_text(wanted)


def read_env() -> dict[str, str]:
    values: dict[str, str] = {}
    if not ENV_FILE.exists():
        return values
    for line in ENV_FILE.read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        key, _, value = line.partition("=")
        values[key.strip()] = value.strip()
    return values


def write_env(updates: dict[str, str]) -> None:
    """Set keys in .env, keeping every comment and every other line intact."""
    lines = ENV_FILE.read_text(encoding="utf-8").splitlines()
    remaining = dict(updates)
    for index, line in enumerate(lines):
        stripped = line.strip()
        if not stripped or stripped.startswith("#") or "=" not in stripped:
            continue
        key = stripped.partition("=")[0].strip()
        if key in remaining:
            lines[index] = f"{key}={remaining.pop(key)}"
    for key, value in remaining.items():
        lines.append(f"{key}={value}")
    ENV_FILE.write_text("\n".join(lines) + "\n", encoding="utf-8")


def missing_values(values: dict[str, str]) -> list[tuple[str, str]]:
    gaps = []
    for key, prompt in NEEDED:
        if key == "OWNER_ID":
            continue  # the bot can start without it and print it to the log
        value = values.get(key, "")
        if not value or value in PLACEHOLDERS:
            gaps.append((key, prompt))
    return gaps


def ensure_env() -> None:
    if not ENV_FILE.exists():
        if not ENV_EXAMPLE.exists():
            die(f"neither {ENV_FILE.name} nor {ENV_EXAMPLE.name} exists")
        step(f"creating {ENV_FILE.name} from {ENV_EXAMPLE.name}")
        shutil.copyfile(ENV_EXAMPLE, ENV_FILE)

    values = read_env()
    gaps = missing_values(values)
    if not gaps:
        return

    if not sys.stdin.isatty():
        die(
            f"{ENV_FILE.name} is missing: " + ", ".join(key for key, _ in gaps),
            f"Edit {ENV_FILE} and run this again.",
        )

    step(f"filling in {ENV_FILE.name} (Ctrl-C to stop and edit it by hand)")
    answers: dict[str, str] = {}
    for key, prompt in NEEDED:
        current = values.get(key, "")
        if current and current not in PLACEHOLDERS:
            continue
        answer = input(f"    {prompt}\n    {key}=").strip()
        if answer:
            answers[key] = answer
    if answers:
        write_env(answers)

    still = missing_values(read_env())
    if still:
        die(
            "still missing: " + ", ".join(key for key, _ in still),
            f"Edit {ENV_FILE} and run this again.",
        )


def start() -> None:
    if not ENTRY.exists():
        die(f"{ENTRY.name} is missing")
    python = str(venv_python())
    step("starting the bot")
    os.chdir(ROOT)
    try:
        os.execv(python, [python, str(ENTRY)])
    except OSError as exc:  # pragma: no cover - execv rarely fails
        die(f"could not start {ENTRY.name}: {exc}")


def main(argv: list[str]) -> None:
    unknown = [a for a in argv if a not in
               ("--install", "--install-only", "--update", "--recreate", "-h", "--help")]
    if unknown or "-h" in argv or "--help" in argv:
        print(__doc__)
        raise SystemExit(2 if unknown else 0)

    check_python()
    build_venv(recreate="--recreate" in argv)
    install(update="--update" in argv or "--recreate" in argv)
    ensure_env()

    if "--install" in argv or "--install-only" in argv:
        step("ready — start it with: python3 setup.py")
        return
    start()


if __name__ == "__main__":
    try:
        main(sys.argv[1:])
    except KeyboardInterrupt:
        warn("stopped")
        raise SystemExit(130)
