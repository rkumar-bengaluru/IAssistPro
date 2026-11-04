"""Write contracts/*.v1.schema.json from the pydantic models (sorted keys, 2-space indent)."""

import json
from pathlib import Path

from iassistpro.contract import export_schemas

OUT = Path(__file__).resolve().parents[1] / "contracts"

if __name__ == "__main__":
    for name, schema in export_schemas().items():
        (OUT / name).write_text(json.dumps(schema, indent=2, sort_keys=True) + "\n", encoding="utf-8")
        print(f"wrote contracts/{name}")
