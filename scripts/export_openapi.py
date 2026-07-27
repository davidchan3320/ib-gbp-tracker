"""Export the FastAPI OpenAPI schema as a checked-in JSON document."""

import json
from pathlib import Path

from app.main import app

PROJECT_ROOT = Path(__file__).resolve().parents[1]
OUTPUT_PATH = PROJECT_ROOT / "openapi.json"


def main() -> None:
    document = json.dumps(app.openapi(), indent=2, ensure_ascii=False)
    OUTPUT_PATH.write_text(f"{document}\n", encoding="utf-8")
    print(f"Wrote {OUTPUT_PATH.relative_to(PROJECT_ROOT)}")


if __name__ == "__main__":
    main()
