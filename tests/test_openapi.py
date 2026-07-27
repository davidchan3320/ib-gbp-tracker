import json
from pathlib import Path

from app.main import app


def test_checked_in_openapi_document_matches_application() -> None:
    document_path = Path(__file__).parents[1] / "openapi.json"
    checked_in_document = json.loads(document_path.read_text(encoding="utf-8"))

    assert checked_in_document == app.openapi()
