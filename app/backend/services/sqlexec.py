"""Runs SQL on the configured warehouse as the app's service principal, with bound parameters."""
import time

from .. import config

_client = None
POLL_SECONDS = 55  # after the 50 s wait_timeout; the Apps proxy drops a request after about two minutes


def client():
    global _client
    if _client is None:
        from databricks.sdk import WorkspaceClient
        _client = WorkspaceClient()
    return _client


def _execute(statement: str, params: dict | None):
    from databricks.sdk.service.sql import (Disposition, Format, StatementParameterListItem,
                                            StatementState)
    api = client().statement_execution
    resp = api.execute_statement(
        warehouse_id=config.WAREHOUSE_ID, statement=statement, wait_timeout="50s",
        disposition=Disposition.INLINE, format=Format.JSON_ARRAY,
        parameters=[StatementParameterListItem(name=k, type=t, value=str(v))
                    for k, (t, v) in (params or {}).items()])
    # Still queued or running after wait_timeout (a waking warehouse, a long scan): poll instead of failing.
    deadline = time.monotonic() + POLL_SECONDS
    while resp.status.state in (StatementState.PENDING, StatementState.RUNNING):
        if time.monotonic() > deadline:
            api.cancel_execution(resp.statement_id)
            raise RuntimeError("the SQL warehouse did not finish the statement in time")
        time.sleep(0.5)
        resp = api.get_statement(resp.statement_id)
    if resp.status.state != StatementState.SUCCEEDED:
        msg = resp.status.error.message if resp.status.error else str(resp.status.state)
        raise RuntimeError(msg)
    return resp


def _rows(resp) -> list[list]:
    """Every row: an INLINE result over a few MB comes in chunks, and the response holds only the first."""
    rows = list(resp.result.data_array or []) if resp.result else []
    for i in range(1, (resp.manifest.total_chunk_count if resp.manifest else 0) or 1):
        rows += client().statement_execution.get_statement_result_chunk_n(resp.statement_id, i).data_array or []
    return rows


def run(statement: str, params: dict | None = None) -> list[list]:
    """params: {name: (SQL type, value)}. Returns rows as lists of strings/None."""
    return _rows(_execute(statement, params))


def run_dicts(statement: str, params: dict | None = None) -> list[dict]:
    """Like run, but rows keyed by column name (needed for DESCRIBE statements)."""
    resp = _execute(statement, params)
    names = [c.name for c in resp.manifest.schema.columns]
    return [dict(zip(names, row)) for row in _rows(resp)]


def run_typed(statement: str, params: dict | None = None) -> tuple[list[tuple[str, str]], list[list]]:
    """Like run, plus each column's (name, type) as the API names the type: STRING, LONG, DOUBLE, TIMESTAMP, ..."""
    resp = _execute(statement, params)
    cols = [(c.name, c.type_name.value if c.type_name else "STRING") for c in resp.manifest.schema.columns]
    return cols, _rows(resp)
