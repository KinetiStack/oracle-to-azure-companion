# =============================================================================
# Azure Function consumer for the files_to_emit queue (Ch.5 deferred consumer).
#
# Trigger:   Timer (every 60s by default; see function.json).
# Runtime:   Python 3.11+ on Azure Functions Premium or Consumption plan.
#
# Idempotency design (P23 in Ch.11 § 11.2):
#   1. Claim a batch of rows atomically via FOR UPDATE SKIP LOCKED so two
#      consumer instances never process the same row.
#   2. Write the blob with a DETERMINISTIC name derived from emit_id, so a
#      crash-and-retry overwrites itself rather than producing duplicates.
#   3. Mark the row status='SENT' + consumed_at in the SAME transaction
#      that holds the FOR UPDATE -- commit binds blob-write to status flip.
#
# Engine dispatch:
#   - QUEUE_ENGINE env var picks 'mssql' (Azure SQL DB / MI) or 'pg' (PG Flex).
#   - Both engines use FOR UPDATE SKIP LOCKED; T-SQL's READPAST hint is the
#     SQL Server equivalent and applied through the WITH (...) clause.
# =============================================================================
import logging
import os
from datetime import datetime, timezone

import azure.functions as func
from azure.identity import DefaultAzureCredential
from azure.storage.blob import BlobServiceClient

log = logging.getLogger(__name__)

QUEUE_ENGINE        = os.environ["QUEUE_ENGINE"]               # 'mssql' or 'pg'
DB_HOST             = os.environ["DB_HOST"]
DB_NAME             = os.environ["DB_NAME"]
DB_USER             = os.environ["DB_USER"]
DB_PASSWORD         = os.environ["DB_PASSWORD"]
BLOB_ACCOUNT_URL    = os.environ["BLOB_ACCOUNT_URL"]            # https://acct.blob.core.windows.net
BLOB_CONTAINER      = os.environ.get("BLOB_CONTAINER", "files-to-emit")
BATCH_SIZE          = int(os.environ.get("BATCH_SIZE", "50"))


def _connect():
    if QUEUE_ENGINE == "mssql":
        import pyodbc
        cs = (f"DRIVER={{ODBC Driver 18 for SQL Server}};"
              f"SERVER=tcp:{DB_HOST},1433;DATABASE={DB_NAME};"
              f"UID={DB_USER};PWD={DB_PASSWORD};"
              f"Encrypt=yes;TrustServerCertificate=no")
        return pyodbc.connect(cs, autocommit=False)
    if QUEUE_ENGINE == "pg":
        import psycopg
        return psycopg.connect(host=DB_HOST, dbname=DB_NAME,
                               user=DB_USER, password=DB_PASSWORD,
                               sslmode="require", autocommit=False)
    raise ValueError(f"unknown QUEUE_ENGINE: {QUEUE_ENGINE}")


def _claim_batch(conn) -> list[tuple]:
    """Atomically claim up to BATCH_SIZE pending rows.

    Returns list of (emit_id, run_id, file_name, payload). Rows remain
    locked for the rest of the transaction; releasing happens at
    commit/rollback in process_batch().
    """
    cur = conn.cursor()
    if QUEUE_ENGINE == "mssql":
        # READPAST + UPDLOCK + ROWLOCK is the T-SQL equivalent of
        # FOR UPDATE SKIP LOCKED. We use TOP (N) for batching.
        cur.execute(f"""
            SELECT TOP ({BATCH_SIZE}) emit_id, run_id, file_name, payload
              FROM dbo.files_to_emit WITH (UPDLOCK, READPAST, ROWLOCK)
             WHERE status = 'PENDING'
             ORDER BY emit_id
        """)
    else:  # pg
        cur.execute("""
            SELECT emit_id, run_id, file_name, payload
              FROM hrpro.files_to_emit
             WHERE status = 'PENDING'
             ORDER BY emit_id
             LIMIT %s
             FOR UPDATE SKIP LOCKED
        """, (BATCH_SIZE,))
    return cur.fetchall()


def _mark_sent(conn, emit_ids: list[int]) -> None:
    if not emit_ids:
        return
    cur = conn.cursor()
    if QUEUE_ENGINE == "mssql":
        placeholders = ",".join("?" * len(emit_ids))
        cur.execute(
            f"UPDATE dbo.files_to_emit "
            f"   SET status = 'SENT', consumed_at = SYSUTCDATETIME() "
            f" WHERE emit_id IN ({placeholders})", *emit_ids)
    else:
        cur.execute(
            "UPDATE hrpro.files_to_emit "
            "   SET status = 'SENT', consumed_at = clock_timestamp() "
            " WHERE emit_id = ANY(%s)", (emit_ids,))


def _blob_name(run_id: int, emit_id: int, file_name: str) -> str:
    """Deterministic blob path: idempotent on retry by design.

    Layout: {run_id}/{emit_id}-{file_name}
    If the function crashes after upload but before the status flip, the
    next invocation re-uploads to the same path -- overwrite semantics on
    Azure Blob means no duplicate file appears.
    """
    safe_name = file_name.replace("/", "_").replace("..", "_")
    return f"{run_id}/{emit_id}-{safe_name}"


def process_batch() -> int:
    """One iteration: claim, write, mark, commit. Returns rows processed."""
    cred = DefaultAzureCredential()
    blob = BlobServiceClient(account_url=BLOB_ACCOUNT_URL, credential=cred)
    container_client = blob.get_container_client(BLOB_CONTAINER)
    try:
        container_client.create_container()
    except Exception:
        pass    # already exists

    processed = 0
    with _connect() as conn:
        try:
            rows = _claim_batch(conn)
            if not rows:
                conn.rollback()
                return 0

            emit_ids: list[int] = []
            for emit_id, run_id, file_name, payload in rows:
                blob_path = _blob_name(run_id, emit_id, file_name)
                container_client.upload_blob(
                    name=blob_path,
                    data=(payload.encode("utf-8") if isinstance(payload, str) else payload),
                    overwrite=True,    # idempotent on retry
                )
                emit_ids.append(emit_id)
                log.info("uploaded emit_id=%s -> %s/%s",
                         emit_id, BLOB_CONTAINER, blob_path)

            _mark_sent(conn, emit_ids)
            conn.commit()
            processed = len(emit_ids)
        except Exception:
            conn.rollback()
            log.exception("batch failed; rolled back -- rows return to PENDING")
            raise
    return processed


def main(timer: func.TimerRequest) -> None:
    if timer.past_due:
        log.warning("timer past due; consumer is falling behind")
    start = datetime.now(timezone.utc)
    n = process_batch()
    elapsed = (datetime.now(timezone.utc) - start).total_seconds()
    log.info("processed %d files in %.2fs", n, elapsed)
