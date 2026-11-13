"""Print the pip requirements of the core yamaha_musiccast integration."""

import json
from pathlib import Path

import homeassistant.components.yamaha_musiccast as mc

print(
    "\n".join(json.loads((Path(mc.__file__).parent / "manifest.json").read_text())["requirements"])
)
