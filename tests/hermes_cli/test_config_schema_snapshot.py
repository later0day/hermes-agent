"""The dashboard's config-schema snapshot (web/src/i18n/config-schema-snapshot.json) names only fields
the backend still serves, under the category it serves them in. The zh Config-page overlay test checks
its entries against that snapshot, so a stale snapshot would let stale translations pass."""

import json
from pathlib import Path

from hermes_cli.web_server_config import CONFIG_SCHEMA

SNAPSHOT = Path(__file__).resolve().parents[2] / "web" / "src" / "i18n" / "config-schema-snapshot.json"


def test_snapshot_paths_and_categories_exist_in_the_served_schema():
    snapshot = json.loads(SNAPSHOT.read_text(encoding="utf-8"))
    stale = {path: cat for path, cat in snapshot.items() if CONFIG_SCHEMA.get(path, {}).get("category") != cat}
    assert stale == {}, "regenerate config-schema-snapshot.json from CONFIG_SCHEMA and drop the zh overlay entries it orphans"
