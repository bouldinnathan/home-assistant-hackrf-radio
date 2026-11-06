"""Build a manual-install ZIP containing only the versioned integration."""

import json
from pathlib import Path
from zipfile import ZIP_DEFLATED, ZipFile

root = Path(__file__).resolve().parents[1]
component = root / "custom_components/hackrf_radio"
version = json.loads((component / "manifest.json").read_text())["version"]
destination = root / "dist" / f"hackrf_radio-{version}.zip"
destination.parent.mkdir(exist_ok=True)
with ZipFile(destination, "w", ZIP_DEFLATED) as archive:
    for path in sorted(component.rglob("*")):
        if path.is_file() and "__pycache__" not in path.parts and path.suffix not in (".pyc", ".pyo"):
            archive.write(path, path.relative_to(component.parent))
print(destination)
