"""Print the pip requirements of the core yamaha_musiccast integration.

Reads its manifest from disk: importing the package would already need them.
"""

import importlib.util
import json
from pathlib import Path

spec = importlib.util.find_spec("homeassistant")
manifest = Path(spec.origin).parent / "components" / "yamaha_musiccast" / "manifest.json"
print("\n".join(json.loads(manifest.read_text())["requirements"]))
