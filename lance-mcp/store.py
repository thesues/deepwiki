"""The LanceDB store and the one table both ingesters share.

`files` holds every indexed file's full text. It is what lets read_file work
without reopening corpus files: search and reads both come from Lance tables
that live in autumn — reached as an ordinary filesystem path (local dev, and
the FUSE deployments this replaced) or as an `s3://` URI through the
autumn-s3 gateway. The two see the same bytes: `s3://lancedb/buda` and the
mount path `/mnt/autumn/lancedb/buda` are the same objects, so a table
written by either is read by the other without re-ingesting.
"""
from datetime import timedelta
from pathlib import Path

import lancedb
import pyarrow as pa

DIM = 1024  # BGE-M3

# object_store's AmazonS3 config, spelled the way lancedb forwards it. The
# gateway neither checks signatures nor serves TLS, and path-style addressing
# is the default.
def s3_storage_options(endpoint: str) -> dict[str, str]:
    return {"endpoint": endpoint.rstrip("/"), "region": "us-east-1",
            "allow_http": "true", "skip_signature": "true"}


def is_s3_uri(path: str | Path) -> bool:
    return str(path).startswith("s3://")

FILES_SCHEMA = pa.schema([
    pa.field("path", pa.string()),
    pa.field("corpus", pa.string()),  # "docs" | "code"
    pa.field("name", pa.string()),
    pa.field("lines", pa.int32()),
    pa.field("text", pa.string()),
])


def connect(db_path: str | Path,
            read_consistency_interval: timedelta | None = None,
            storage_options: dict[str, str] | None = None):
    """Open unmodified community LanceDB.

    `db_path` is either an absolute filesystem path (local dev, or the FUSE
    deployments) or an `s3://bucket/prefix` URI against the autumn-s3
    gateway, in which case `storage_options` carries the gateway endpoint.
    Keeping the storage boundary here means LanceDB needs no Autumn provider,
    fork or Python binding on either path.
    """
    if is_s3_uri(db_path):
        if storage_options is None:
            raise ValueError("an s3:// db path needs the gateway's storage options")
        return lancedb.connect(str(db_path), storage_options=storage_options,
                               read_consistency_interval=read_consistency_interval)
    path = Path(db_path).expanduser()
    if not path.is_absolute():
        raise ValueError(f"LanceDB path must be absolute: {path}")
    return lancedb.connect(str(path), read_consistency_interval=read_consistency_interval)


def rust_lines(text: str) -> list[str]:
    """str::lines(): split on \\n, drop one trailing \\r, no final empty line.

    Line numbers in every id and hit come from this rule, so read_file has to
    count by it too or `start`/`end` would point one line off.
    """
    lines = text.split("\n")
    if lines and lines[-1] == "":
        lines.pop()
    return [l[:-1] if l.endswith("\r") else l for l in lines]


def lit(s: str) -> str:
    """A SQL string literal for a Lance filter."""
    return "'" + s.replace("'", "''") + "'"


def table_names(db) -> set[str]:
    names, token = set(), None
    while True:
        r = db.list_tables(page_token=token)
        names.update(r.tables)
        token = r.page_token
        if not token:
            return names


def replace_files(db, corpus: str, rows: list[dict], under: str | None = None) -> None:
    """Swap one corpus's rows (or those under a path prefix) for `rows`."""
    tbl = pa.Table.from_pylist(rows, schema=FILES_SCHEMA)
    if "files" not in table_names(db):
        db.create_table("files", tbl)
        return
    t = db.open_table("files")
    cond = f"corpus = {lit(corpus)}"
    if under is not None:
        cond += f" AND starts_with(path, {lit(under)})"
    t.delete(cond)
    if rows:
        t.add(tbl)
