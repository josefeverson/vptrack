#!/usr/bin/env python3
"""Build a local macOS .app wrapper for the YveScanner tracker."""

from __future__ import annotations

import argparse
import json
import plistlib
import shutil
import shlex
import sqlite3
import stat
import subprocess
import sys
from pathlib import Path


APP_NAME = "YveScanner"
BUNDLE_ID = "local.yvescanner.tracker"
VERSION = "0.1.0"


def build_launcher(python_executable: Path) -> str:
    quoted_python = shlex.quote(str(python_executable))
    return f"""#!/bin/zsh
set -e

MACOS_DIR="$(cd "$(dirname "$0")" && pwd)"
CONTENTS_DIR="$(dirname "$MACOS_DIR")"
APP_RESOURCES="$CONTENTS_DIR/Resources/app"
PYTHON={quoted_python}
SUPPORT_DIR="$HOME/Library/Application Support/YveScanner"
LOG_DIR="$HOME/Library/Logs/YveScanner"
TRACKER="$APP_RESOURCES/vp_tracker.py"
CONFIG="$SUPPORT_DIR/config.json"
DATABASE="$SUPPORT_DIR/vp_tracker.sqlite3"

fail_alert() {{
  /usr/bin/osascript -e 'display alert "YveScanner cannot start" message "'"$1"'" as critical' >/dev/null 2>&1 || true
  echo "YveScanner cannot start: $1" >&2
  exit 1
}}

if [ ! -x "$PYTHON" ]; then
  fail_alert "The Python runtime used to build this app is missing. Rebuild YveScanner.app."
fi

if [ ! -f "$TRACKER" ]; then
  fail_alert "The bundled tracker file is missing. Rebuild YveScanner.app."
fi

mkdir -p "$SUPPORT_DIR" "$LOG_DIR"

if [ ! -f "$CONFIG" ]; then
  cp "$APP_RESOURCES/config.seed.json" "$CONFIG" || fail_alert "Could not create the local tracker config."
fi

if [ ! -f "$DATABASE" ] && [ -f "$APP_RESOURCES/vp_tracker.seed.sqlite3" ]; then
  cp "$APP_RESOURCES/vp_tracker.seed.sqlite3" "$DATABASE" || true
fi

cd "$SUPPORT_DIR" || fail_alert "Could not open the local support folder."
exec "$PYTHON" -u "$TRACKER" --config "$CONFIG" --daemon --gui >> "$LOG_DIR/yvescanner-app.log" 2>> "$LOG_DIR/yvescanner-app.err.log"
"""


def discover_python_runtime(project_root: Path) -> Path:
    venv_cfg = project_root / ".venv" / "pyvenv.cfg"
    if venv_cfg.exists():
        for line in venv_cfg.read_text(encoding="utf-8").splitlines():
            key, separator, value = line.partition("=")
            if key.strip() == "executable" and separator:
                candidate = Path(value.strip())
                if candidate.exists():
                    return candidate

    base_executable = Path(getattr(sys, "_base_executable", "") or "")
    if base_executable.exists() and ".venv" not in base_executable.parts:
        return base_executable

    current_executable = Path(sys.executable)
    if current_executable.exists():
        return current_executable

    return Path("/usr/bin/python3")


def write_config_seed(project_root: Path, resources: Path) -> None:
    source = project_root / "config.json"
    if not source.exists():
        source = project_root / "config.example.json"

    with source.open("r", encoding="utf-8") as fh:
        config = json.load(fh)

    config["database_path"] = "vp_tracker.sqlite3"

    with (resources / "config.seed.json").open("w", encoding="utf-8") as fh:
        json.dump(config, fh, indent=2)
        fh.write("\n")


def write_database_seed(project_root: Path, resources: Path) -> None:
    source = project_root / "vp_tracker.sqlite3"
    if not source.exists():
        return

    destination = resources / "vp_tracker.seed.sqlite3"
    if destination.exists():
        destination.unlink()

    source_db = sqlite3.connect(f"file:{source}?mode=ro", uri=True)
    try:
        destination_db = sqlite3.connect(destination)
        try:
            source_db.backup(destination_db)
        finally:
            destination_db.close()
    finally:
        source_db.close()


def build_app(project_root: Path, output_dir: Path) -> Path:
    app_path = output_dir / f"{APP_NAME}.app"
    contents = app_path / "Contents"
    macos = contents / "MacOS"
    resources = contents / "Resources" / "app"

    if app_path.exists():
        shutil.rmtree(app_path)

    macos.mkdir(parents=True)
    resources.mkdir(parents=True)

    shutil.copy2(project_root / "vp_tracker.py", resources / "vp_tracker.py")
    write_config_seed(project_root, resources)
    write_database_seed(project_root, resources)

    info = {
        "CFBundleDevelopmentRegion": "en",
        "CFBundleDisplayName": APP_NAME,
        "CFBundleExecutable": APP_NAME,
        "CFBundleIdentifier": BUNDLE_ID,
        "CFBundleInfoDictionaryVersion": "6.0",
        "CFBundleName": APP_NAME,
        "CFBundlePackageType": "APPL",
        "CFBundleShortVersionString": VERSION,
        "CFBundleVersion": VERSION,
        "LSMinimumSystemVersion": "11.0",
        "NSDesktopFolderUsageDescription": (
            "YveScanner may read local tracker files if this project is kept on the Desktop."
        ),
        "NSDocumentsFolderUsageDescription": (
            "YveScanner reads its local tracker files and config from this project folder."
        ),
        "NSDownloadsFolderUsageDescription": (
            "YveScanner may read local tracker files if this project is kept in Downloads."
        ),
        "NSHighResolutionCapable": True,
        "NSUserSelectedFileReadWriteUsageDescription": (
            "YveScanner reads and writes only its local tracker config, database, and logs."
        ),
    }

    with (contents / "Info.plist").open("wb") as fh:
        plistlib.dump(info, fh, sort_keys=False)

    (contents / "PkgInfo").write_text("APPL????", encoding="utf-8")

    launcher = macos / APP_NAME
    launcher.write_text(build_launcher(discover_python_runtime(project_root)), encoding="utf-8")
    launcher.chmod(launcher.stat().st_mode | stat.S_IXUSR | stat.S_IXGRP | stat.S_IXOTH)

    return app_path


def sign_app(app_path: Path) -> None:
    codesign = Path("/usr/bin/codesign")
    if not codesign.exists():
        print("codesign not found; app bundle was built without ad-hoc signing.")
        return

    result = subprocess.run(
        [str(codesign), "--force", "--deep", "--sign", "-", str(app_path)],
        check=False,
        capture_output=True,
        text=True,
    )
    if result.returncode == 0:
        print("Ad-hoc signed app bundle for local macOS launch.")
        return

    detail = (result.stderr or result.stdout).strip()
    print(f"codesign warning: {detail}")


def main() -> int:
    parser = argparse.ArgumentParser(description="Build the local YveScanner macOS app bundle.")
    parser.add_argument(
        "--output-dir",
        type=Path,
        default=Path("dist"),
        help="Directory to place the .app bundle in (default: dist).",
    )
    args = parser.parse_args()

    project_root = Path(__file__).resolve().parents[1]
    output_dir = (project_root / args.output_dir).resolve()
    output_dir.mkdir(parents=True, exist_ok=True)

    app_path = build_app(project_root, output_dir)
    sign_app(app_path)
    print(f"Built {app_path}")
    print(f"Open it with: open {shlex.quote(str(app_path))}")
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
