"""Build a source-only AstrBot plugin ZIP, excluding runtime and test data."""

from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
destination = root / "dist" / "astrbot_plugin_custom_plan-v0.1.0.zip"
destination.parent.mkdir(parents=True, exist_ok=True)
files = list(root.glob("*.py"))
files += [
    root / name
    for name in ("metadata.yaml", "requirements.txt", "_conf_schema.json", "README.md")
]
for directory in ("templates", "assets"):
    files += [file for file in (root / directory).rglob("*") if file.is_file()]
with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
    for file in sorted(files):
        archive.write(file, file.relative_to(root).as_posix())
with ZipFile(destination) as archive:
    assert archive.testzip() is None
print(destination)
