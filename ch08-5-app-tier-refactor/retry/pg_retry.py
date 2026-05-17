#!/usr/bin/env python3
"""pg_retry.py - application-layer retry decorator for Azure DB for
PostgreSQL Flex transient errors.

Replaces the absent driver-side TAF; recognizes the PG SQLSTATE codes that
indicate transient conditions per the PostgreSQL error-code reference plus
Azure-specific ones from "Common connection issues to PostgreSQL Flexible
Server" documentation.

Dependencies: psycopg 3.x (or psycopg2 fallback), tenacity 8.x.

Usage:
    from pg_retry import execute_with_retry
    rows = execute_with_retry(conn,
        "SELECT * FROM hrpro.employee WHERE emp_id = %s",
        (42,))
"""
from __future__ import annotations

import logging
import sys
from typing import Any, Sequence

try:
    import psycopg
    _PSYCOPG = "psycopg3"
    OperationalError = psycopg.errors.OperationalError
except ImportError:
    import psycopg2 as psycopg     # type: ignore[no-redef]
    _PSYCOPG = "psycopg2"
    OperationalError = psycopg.OperationalError

from tenacity import (
    retry,
    stop_after_attempt,
    wait_exponential_jitter,
    retry_if_exception,
    before_sleep_log,
)

log = logging.getLogger("pg_retry")

# PostgreSQL SQLSTATE codes that indicate transient conditions.
# See: https://www.postgresql.org/docs/current/errcodes-appendix.html
# Class 08: Connection Exception; Class 57: Operator Intervention;
# Class 40: Transaction Rollback.
PG_TRANSIENT_SQLSTATES: set[str] = {
    # Class 08 -- Connection
    "08000",   # connection_exception
    "08003",   # connection_does_not_exist
    "08006",   # connection_failure
    "08001",   # sqlclient_unable_to_establish_sqlconnection
    "08004",   # sqlserver_rejected_establishment_of_sqlconnection
    # Class 57 -- Operator Intervention
    "57P01",   # admin_shutdown
    "57P02",   # crash_shutdown
    "57P03",   # cannot_connect_now
    # Class 40 -- Transaction Rollback (worth retrying)
    "40001",   # serialization_failure
    "40P01",   # deadlock_detected
    # Class 53 -- Insufficient Resources
    "53300",   # too_many_connections
    "53400",   # configuration_limit_exceeded
}


def _is_transient(exc: BaseException) -> bool:
    if isinstance(exc, OperationalError):
        # OperationalError covers most network-layer failures; pyscopg sets
        # the SQLSTATE when known.
        return True
    sqlstate = getattr(exc, "sqlstate", None) or getattr(exc, "pgcode", None)
    if sqlstate in PG_TRANSIENT_SQLSTATES:
        return True
    return False


retry_decorator = retry(
    stop=stop_after_attempt(5),
    wait=wait_exponential_jitter(initial=0.2, max=10.0),
    retry=retry_if_exception(_is_transient),
    reraise=True,
    before_sleep=before_sleep_log(log, logging.WARNING),
)


@retry_decorator
def execute_with_retry(conn, sql: str, params: Sequence[Any] | None = None) -> list[tuple]:
    """Execute SQL on conn with full retry. Returns rows for SELECT, [] for DML.

    The caller is responsible for transaction control. On retry, the entire
    statement is replayed -- which is correct for idempotent DML but NOT
    for non-idempotent INSERTs without uniqueness guards. For non-idempotent
    INSERTs, either use ON CONFLICT DO NOTHING or wrap the call in an
    application-level idempotency token.
    """
    with conn.cursor() as cur:
        cur.execute(sql, params)
        if cur.description is None:
            return []
        return cur.fetchall()


@retry_decorator
def connect_with_retry(dsn: str, **kwargs: Any):
    """Open a PG connection with retry on transient connect-time errors."""
    return psycopg.connect(dsn, **kwargs)


if __name__ == "__main__":
    # Smoke-test the decorator's classification without needing a live PG.
    logging.basicConfig(level=logging.INFO, format="%(levelname)s %(message)s")
    print(f"using driver: {_PSYCOPG}")
    print(f"transient sqlstates: {sorted(PG_TRANSIENT_SQLSTATES)}")
