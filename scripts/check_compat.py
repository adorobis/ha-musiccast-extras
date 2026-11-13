"""Check that the installed Home Assistant still has what the patch relies on.

Run in CI after installing the newest homeassistant and the aiomusiccast
version its yamaha_musiccast integration pins. Exits 1 and lists what is
missing, so a breaking Home Assistant change shows up before users hit it.
"""

from pathlib import Path
import sys

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))

from custom_components.musiccast_extras import patch

missing = patch.missing_hooks()
if missing:
    print("Missing:", *missing, sep="\n  ")
    sys.exit(1)
patch.apply()
patch.remove()
print("All hooks present; patch applies and reverts cleanly.")
