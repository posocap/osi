"""Managed workflow worker entrypoint."""

import asyncio

import mistralai.workflows as workflows

from workflows.d1_backup import D1BigQueryBackupWorkflow

if __name__ == "__main__":
    asyncio.run(workflows.run_worker([D1BigQueryBackupWorkflow]))
