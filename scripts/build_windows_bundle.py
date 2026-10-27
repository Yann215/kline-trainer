"""Build a Windows resource ZIP from tracked source and a built frontend.

Run after ``npm run build``. Local environments and training data are excluded.
"""
from __future__ import annotations

import argparse
import subprocess
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile, ZipInfo

ROOT = Path(__file__).resolve().parents[1]
EXTRA = ("desktop_launcher.pyw", "windows-start.cmd", "scripts/windows-start.ps1",
         "scripts/build_windows_bundle.py")


def main() -> None:
    parser = argparse.ArgumentParser()
    parser.add_argument("--output", type=Path, default=ROOT / "local-artifacts" / "kline-trainer-windows.zip")
    args = parser.parse_args()
    if not (ROOT / "frontend/dist/index.html").is_file():
        parser.error("frontend/dist is missing; run npm run build in frontend first")
    tracked = subprocess.check_output(["git", "ls-files", "-z"], cwd=ROOT).decode("utf-8").split("\0")
    # The published ZIP is itself tracked; never recursively include an older bundle.
    names = {name for name in tracked if name and Path(name).parts[0] != "downloads"} | set(EXTRA)
    names |= {path.relative_to(ROOT).as_posix() for path in (ROOT / "frontend/dist").rglob("*") if path.is_file()}
    # Explicit denylist protects against accidental inclusion if tracked files change.
    forbidden_dirs = {".git", ".venv", "venv", "node_modules", "runtime", "data", "local-artifacts", "__pycache__"}
    forbidden_suffixes = {".sqlite", ".sqlite3", ".db", ".parquet", ".csv", ".jsonl", ".log", ".pyc"}
    for name in names:
        path = Path(name)
        if (set(path.parts) & forbidden_dirs or path.suffix.lower() in forbidden_suffixes
                or (path.name.startswith(".env") and path.name != ".env.example")):
            raise ValueError(f"Refusing to package local or sensitive file: {name}")
        if not (ROOT / path).is_file():
            raise FileNotFoundError(name)
    args.output.parent.mkdir(parents=True, exist_ok=True)
    with ZipFile(args.output, "w", compression=ZIP_DEFLATED, compresslevel=9) as archive:
        for name in sorted(names):
            info = ZipInfo("kline-trainer/" + name, date_time=(2020, 1, 1, 0, 0, 0))
            info.compress_type = ZIP_DEFLATED
            info.external_attr = 0o100644 << 16
            archive.writestr(info, (ROOT / name).read_bytes(), compresslevel=9)
    print(f"Built {args.output} ({len(names)} files, {args.output.stat().st_size} bytes)")


if __name__ == "__main__":
    main()
