"""Print the pip requirements of the core yamaha_musiccast integration.

Includes the requirements of the integrations it depends on (e.g. ssdp),
which its modules import. Reads the manifests from disk: importing the
packages would already need the requirements.
"""

import importlib.util
import json
from pathlib import Path

spec = importlib.util.find_spec("homeassistant")
components = Path(spec.origin).parent / "components"

requirements: list[str] = []
seen: set[str] = set()
todo = ["yamaha_musiccast"]
while todo:
    domain = todo.pop()
    if domain in seen:
        continue
    seen.add(domain)
    manifest = json.loads((components / domain / "manifest.json").read_text())
    requirements += [r for r in manifest.get("requirements", []) if r not in requirements]
    todo += manifest.get("dependencies", [])
print("\n".join(requirements))
