"""Scheduled refresh of the BigQuery backup of the whoneedsit D1 database.

Rebuilds every table of the Cloudflare D1 database in the BigQuery dataset
using CREATE OR REPLACE TABLE ... AS SELECT * FROM UNNEST statements only
(the BigQuery project is on the free tier where DML is blocked), then
verifies per-table row counts against D1.

Cloudflare and BigQuery credentials are resolved at runtime from the
triggering user connected Mistral connectors; nothing is stored here.
"""

import json
from typing import Any

import pydantic
import mistralai.workflows as workflows
from mistralai.workflows import Depends
from mistralai.workflows.plugins.mistralai.connectors import (
    ToolCallClient,
    connector,
    uses_connectors,
)

cf_connector = connector("cloudflare_developer_platform_cf27")
bq_connector = connector("bigquery_")

DEFAULT_DATABASE_ID = "82c55bae-2ae2-4502-bd8e-1a790ba981c4"
DEFAULT_PROJECT = "peikoff-net"
DEFAULT_DATASET = "peikoff-net.Objectivism"

SMALL_CHUNK_TABLES = {"articles", "questions"}
SMALL_CHUNK = 120
BIG_CHUNK = 800
MAX_STATEMENT_CHARS = 440000

BS = chr(92)
BT = chr(96)


def q(path: str) -> str:
    """Backtick-quoted BigQuery identifier path."""
    return BT + path + BT


def unwrap(resp: Any) -> Any:
    if isinstance(resp, list):
        resp = resp[0] if resp else None
    if isinstance(resp, dict):
        content = resp.get("content")
        if isinstance(content, list) and content and isinstance(content[0], dict):
            text = content[0].get("text")
            if isinstance(text, str):
                try:
                    resp = json.loads(text)
                except (ValueError, TypeError):
                    return resp
        if isinstance(resp, list):
            resp = resp[0] if resp else None
    if isinstance(resp, str):
        try:
            resp = json.loads(resp)
        except (ValueError, TypeError):
            return resp
    if isinstance(resp, list):
        resp = resp[0] if resp else None
    return resp


def d1_results(resp: Any) -> list:
    o = unwrap(resp)
    if isinstance(o, dict) and isinstance(o.get("results"), list):
        return o["results"]
    if isinstance(o, list):
        return o
    raise RuntimeError("Unrecognized D1 response: " + str(o)[:400])


def bq_check(resp: Any) -> Any:
    o = unwrap(resp)
    if isinstance(o, dict) and o.get("errors"):
        raise RuntimeError("BigQuery error: " + json.dumps(o["errors"])[:600])
    return o


def bq_count_value(resp: Any) -> int:
    o = bq_check(resp)
    rows = (o or {}).get("rows") or []
    if not rows:
        raise RuntimeError("BigQuery returned no count rows: " + str(o)[:400])
    first = rows[0]
    if isinstance(first, dict) and isinstance(first.get("f"), list):
        return int(first["f"][0]["v"])
    return int(next(iter(first.values())))


def bq_escape(s: str) -> str:
    out = s.replace(BS, BS + BS)
    out = out.replace(chr(39), BS + chr(39))
    out = out.replace(chr(10), BS + "n")
    out = out.replace(chr(13), BS + "r")
    out = out.replace(chr(9), BS + "t")
    return out


def bq_type(declared: str) -> str:
    d = (declared or "").upper()
    if "INT" in d:
        return "INT64"
    for kw in ("REAL", "FLOA", "DOUB", "NUM"):
        if kw in d:
            return "FLOAT64"
    return "STRING"


def literal(value: Any, col_type: str, col: str) -> str:
    if value is None:
        return "NULL"
    if col_type == "STRING":
        if isinstance(value, str):
            return chr(39) + bq_escape(value) + chr(39)
        return "CAST(" + str(value) + " AS STRING)"
    if isinstance(value, bool):
        return "1" if value else "0"
    if isinstance(value, str):
        raise RuntimeError(
            "Unexpected string in " + col_type + " column " + col + ": " + value[:80]
        )
    if col_type == "INT64":
        if isinstance(value, float):
            if value != int(value):
                raise RuntimeError("Non-integral value in INT64 column " + col)
            return str(int(value))
        return str(value)
    return repr(value) if isinstance(value, float) else str(value)


@workflows.activity(name="d1-bigquery-backup-run")
async def run_backup(
    database_id: str,
    project: str,
    dataset: str,
    cf: ToolCallClient = Depends(cf_connector),
    bq: ToolCallClient = Depends(bq_connector),
) -> dict:
    async def d1(sql: str) -> list:
        resp = await cf.call_tool(
            "d1_database_query", {"database_id": database_id, "sql": sql}
        )
        return d1_results(resp)

    async def bq_sql(sql: str) -> Any:
        return bq_check(
            await bq.call_tool(
                "execute_sql", {"projectId": project, "query": sql, "timeoutMs": 120000}
            )
        )

    async def bq_count(sql: str) -> int:
        resp = await bq.call_tool(
            "execute_sql", {"projectId": project, "query": sql, "timeoutMs": 120000}
        )
        return bq_count_value(resp)

    rows = await d1(
        "SELECT name FROM sqlite_master WHERE type = " + chr(39) + "table" + chr(39)
    )
    tables = [
        r["name"]
        for r in rows
        if not r["name"].startswith("sqlite_") and not r["name"].startswith("_cf")
    ]

    summary: dict = {}
    for table in tables:
        schema_rows = await d1(
            "SELECT p.name, p.type FROM pragma_table_info("
            + chr(39) + table + chr(39) + ") p ORDER BY p.cid"
        )
        cols = [(r["name"], bq_type(r.get("type") or "")) for r in schema_rows]
        if not cols:
            continue
        struct_type = "STRUCT<" + ", ".join(c + " " + t for c, t in cols) + ">"
        d1_count = int(
            (await d1(
                "SELECT COUNT(*) AS n FROM " + chr(34) + table + chr(34)
            ))[0]["n"]
        )

        chunk = SMALL_CHUNK if table in SMALL_CHUNK_TABLES else BIG_CHUNK
        stages: list = []
        offset = 0
        chunk_idx = 0
        while True:
            rows = await d1(
                "SELECT * FROM " + chr(34) + table + chr(34)
                + " ORDER BY rowid LIMIT " + str(chunk) + " OFFSET " + str(offset)
            )
            if not rows:
                break
            tuples = [
                "(" + ", ".join(literal(row.get(c), t, c) for c, t in cols) + ")"
                for row in rows
            ]
            groups: list = []
            current: list = []
            length = 0
            for tup in tuples:
                if current and length + len(tup) + 2 > MAX_STATEMENT_CHARS:
                    groups.append(current)
                    current = []
                    length = 0
                current.append(tup)
                length += len(tup) + 2
            if current:
                groups.append(current)

            for g_idx, group in enumerate(groups):
                name = (
                    table + "__k" + str(offset)
                    + "_i" + str(chunk_idx)
                    + "_j" + str(g_idx)
                )
                stages.append(name)
                await bq_sql(
                    "CREATE OR REPLACE TABLE " + q(dataset + "." + name) + " AS "
                    + "SELECT * FROM UNNEST([" + struct_type + ", ".join(group) + "])"
                )
            if len(rows) < chunk:
                break
            offset += chunk
            chunk_idx += 1

        if not stages:
            ddl_cols = ", ".join(c + " " + t for c, t in cols)
            await bq_sql(
                "CREATE OR REPLACE TABLE "
                + q(dataset + "." + table) + " (" + ddl_cols + ")"
            )
        elif len(stages) == 1:
            await bq_sql(
                "CREATE OR REPLACE TABLE " + q(dataset + "." + table) + " AS "
                + "SELECT * FROM " + q(dataset + "." + stages[0])
            )
            await bq_sql("DROP TABLE " + q(dataset + "." + stages[0]))
        else:
            union = " UNION ALL ".join(
                "SELECT * FROM " + q(dataset + "." + s) for s in stages
            )
            await bq_sql(
                "CREATE OR REPLACE TABLE " + q(dataset + "." + table) + " AS " + union
            )
            for s in stages:
                await bq_sql("DROP TABLE " + q(dataset + "." + s))

        bq_rows = await bq_count(
            "SELECT COUNT(*) AS n FROM " + q(dataset + "." + table)
        )
        if bq_rows != d1_count:
            raise RuntimeError(
                "Row count mismatch for " + table
                + ": D1=" + str(d1_count) + " BigQuery=" + str(bq_rows)
            )
        summary[table] = bq_rows

    return {
        "tables": len(summary),
        "total_rows": sum(summary.values()),
        "counts": summary,
    }


class BackupInput(pydantic.BaseModel):
    database_id: str = DEFAULT_DATABASE_ID
    project: str = DEFAULT_PROJECT
    dataset: str = DEFAULT_DATASET


@workflows.workflow.define(
    name="d1-bigquery-backup",
    workflow_display_name="D1 BigQuery Backup",
    workflow_description=(
        "Refreshes the BigQuery backup of the whoneedsit Cloudflare D1 database "
        "into the peikoff-net.Objectivism dataset and verifies row counts."
    ),
    on_behalf_of=True,
)
@uses_connectors(cf_connector, bq_connector)
class D1BigQueryBackupWorkflow:
    @workflows.workflow.entrypoint
    async def run(self, input: BackupInput) -> dict:
        return await run_backup(input.database_id, input.project, input.dataset)
