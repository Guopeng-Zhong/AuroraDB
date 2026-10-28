"""Import and verify the bundled, MIT-licensed Chinook data snapshot.

The upstream project publishes several database-specific scripts.  This
small adapter keeps the repository's conversion deterministic: the portable
AuroraDB SQL snapshot is committed under ``auroradb/examples/chinook.sql``;
this command loads it into a new directory and records row-count evidence.
It never overwrites an existing database.
"""

from __future__ import annotations

import argparse
import hashlib
import json
import sys
from datetime import datetime, timezone
from pathlib import Path

ROOT = Path(__file__).resolve().parents[1]
sys.path.insert(0, str(ROOT))

from auroradb.engine import Database  # noqa: E402


EXPECTED = {
    "Album": 347,
    "Artist": 275,
    "Customer": 59,
    "Employee": 8,
    "Genre": 25,
    "Invoice": 412,
    "InvoiceLine": 2240,
    "MediaType": 5,
    "Playlist": 18,
    "PlaylistTrack": 8715,
    "Track": 3503,
}


def sha256(path: Path) -> str:
    digest = hashlib.sha256()
    with path.open("rb") as handle:
        for block in iter(lambda: handle.read(1024 * 1024), b""):
            digest.update(block)
    return digest.hexdigest()


def main(argv: list[str] | None = None) -> int:
    parser = argparse.ArgumentParser(description="导入并核验 AuroraDB 的 Chinook 公开数据集")
    parser.add_argument("--db", type=Path, required=True, help="新的数据库目录；已有表时拒绝覆盖")
    parser.add_argument("--evidence", type=Path, default=ROOT / "docs" / "chinook_import.json",
                        help="行数与来源证据 JSON（默认 docs/chinook_import.json）")
    args = parser.parse_args(argv)
    script = ROOT / "auroradb" / "examples" / "chinook.sql"
    if not script.is_file():
        raise SystemExit(f"找不到 Chinook 脚本：{script}")
    args.db.parent.mkdir(parents=True, exist_ok=True)
    database = Database(args.db)
    try:
        if database.schema():
            raise SystemExit("目标数据库已有表；请换一个新目录，避免覆盖数据。")
        database.execute(script.read_text(encoding="utf-8-sig"))
        actual = {
            table: database.execute(f"SELECT COUNT(*) FROM {table}")[0]["rows"][0][0]
            for table in EXPECTED
        }
    finally:
        database.close()
    if actual != EXPECTED:
        raise SystemExit(f"Chinook 行数核验失败：expected={EXPECTED!r}, actual={actual!r}")
    evidence = {
        "dataset": "Chinook",
        "version": "1.4.5",
        "source_url": "https://github.com/lerocha/chinook-database",
        "source_revision": "7f67772503d71ba90f19283c38e93923addb43fa",
        "license": "MIT",
        "snapshot": "auroradb/examples/chinook.sql",
        "snapshot_sha256": sha256(script),
        "adapted_types": {"NVARCHAR": "TEXT", "DATETIME": "TEXT", "NUMERIC": "REAL"},
        "omitted_features": ["foreign-key enforcement", "views", "triggers", "stored procedures", "BLOB/media payloads"],
        "tables": actual,
        "total_rows": sum(actual.values()),
        "database_directory": str(args.db),
        "verified_at": datetime.now(timezone.utc).isoformat(),
    }
    args.evidence.parent.mkdir(parents=True, exist_ok=True)
    args.evidence.write_text(json.dumps(evidence, ensure_ascii=False, indent=2) + "\n", encoding="utf-8")
    print(json.dumps({"db": str(args.db), "tables": len(actual), "total_rows": evidence["total_rows"],
                      "evidence": str(args.evidence)}, ensure_ascii=False))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
