"""Build the dependency-free frontend into the FastAPI static directory."""

import hashlib
import shutil
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
SOURCE = ROOT / "frontend"
TARGET = ROOT / "dist"

TARGET.mkdir(parents=True, exist_ok=True)
assets = (
    ("styles.css", "app.css"),
    ("i18n.js", "i18n.js"),
    ("app.js", "app.js"),
    ("logo.svg", "logo.svg"),
    ("favicon.png", "favicon.ico"),
)
index = (SOURCE / "index.html").read_text(encoding="utf-8")
for source_name, target_name in assets:
    shutil.copy2(SOURCE / source_name, TARGET / target_name)
    digest = hashlib.sha256((SOURCE / source_name).read_bytes()).hexdigest()[:12]
    index = index.replace(f'/{target_name}"', f'/{target_name}?v={digest}"')

(TARGET / "index.html").write_text(index, encoding="utf-8")

print(f"Frontend built into {TARGET}")
