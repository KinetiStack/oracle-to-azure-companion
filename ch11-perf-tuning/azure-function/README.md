# files_to_emit Consumer — Azure Function

Drains the `files_to_emit` queue produced by the refactored
`pkg_payroll_run` (Oracle origin in Ch.3.5's lab schema; refactored to
target-side equivalents in Ch.5) and writes each row's payload as a blob
in the target Storage Account container.

## Files

- `consumer.py` — the function entrypoint. See Ch.11 § 11.2 for the
  three-discipline idempotency design (atomic claim, deterministic blob
  name, status-flip-in-same-transaction).
- `function.json` — Azure Functions trigger binding. Timer trigger fires
  every 60s; `runOnStartup: false` prevents a function execution on every
  deploy.
- `host.json` — Application Insights + extension bundle configuration.

## Required configuration (Function App application settings)

| Setting | Value | Notes |
|---|---|---|
| `QUEUE_ENGINE` | `mssql` or `pg` | Selects the engine-specific claim query |
| `DB_HOST` | FQDN of the target DB | Resolves via the Function App's VNet integration + Private DNS |
| `DB_NAME` | Database / catalog name | `labdb` in the book's reference architecture |
| `DB_USER` | Application-tier DB user | |
| `DB_PASSWORD` | **Key Vault reference** | `@Microsoft.KeyVault(VaultName=...;SecretName=db-password)` |
| `BLOB_ACCOUNT_URL` | Storage account URL | `https://<account>.blob.core.windows.net` |
| `BLOB_CONTAINER` | Container name | `files-to-emit` by default |
| `BATCH_SIZE` | Rows per invocation | `50` is conservative; raise after observing first day's throughput |

## Identity model

Two distinct auth surfaces:

1. **Blob auth** — `DefaultAzureCredential` resolves to the Function App's
   Managed Identity in Azure. Grant the MI the **`Storage Blob Data
   Contributor`** role on the target Storage Account. No static secret.

2. **DB auth** — password-based, with the password delivered to the
   `DB_PASSWORD` environment variable via a Key Vault reference resolved
   at function start. The plaintext exists in the function's process
   memory for the duration of the invocation; this is inherent to
   Oracle / SQL Server / PostgreSQL password-based connections.

   For fully passwordless DB auth (production-grade), switch to Azure AD /
   Entra token authentication on the target side: `Active Directory
   Managed Identity` connection mode on Azure SQL DB / MI; the AAD token
   plugin for PG Flex. Both require driver upgrades and additional
   role-mapping work; covered out-of-scope for this reference function.

## Deployment

The chapter does not ship a Bicep template for this Function App because
Function deployment patterns vary heavily by org (publish-from-CI vs.
container-image; Premium vs Consumption plan; VNet integration topology).
The book's reference uses Premium plan with VNet integration so the
function can reach private-endpoint-only databases.
