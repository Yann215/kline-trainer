"""Collect license texts for frontend runtime dependencies in package-lock.json."""
import json
from pathlib import Path

root = Path(__file__).resolve().parents[1]
frontend = root / "frontend"
lock = json.loads((frontend / "package-lock.json").read_text(encoding="utf-8"))
sections = ["# Frontend third-party notices\n\nGenerated from the runtime dependencies in frontend/package-lock.json.\n"]
for relative, info in sorted(lock["packages"].items()):
    if not relative or info.get("dev") or info.get("optional"):
        continue
    folder = frontend / relative
    meta = json.loads((folder / "package.json").read_text(encoding="utf-8"))
    sections.append(f"\n## {meta['name']} {meta['version']}\n\nLicense: {meta.get('license', 'see package')}\n")
    names = ("LICENSE", "LICENSE.txt", "LICENSE.md", "LICENSE-MIT", "license", "license.md", "COPYING", "NOTICE")
    found = False
    for name in names:
        path = folder / name
        if path.is_file():
            sections.append(f"\n### {name}\n\n```text\n{path.read_text(encoding='utf-8', errors='replace').strip()}\n```\n")
            found = True
    if not found:
        sections.append("\nLicense text is provided in the installed package distribution.\n")
(root / "THIRD_PARTY_NOTICES.md").write_text("".join(sections), encoding="utf-8", newline="\n")
print("Updated THIRD_PARTY_NOTICES.md")
