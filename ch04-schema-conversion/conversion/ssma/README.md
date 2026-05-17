# SSMA for Oracle — Pipeline A (Oracle → Azure SQL Database / MI)

> **Platform constraint — SSMA for Oracle is Windows-only.** Microsoft does not
> publish a Linux or macOS build, and the tool does not run reliably under
> Wine. If your workstation is Linux/macOS, you have three options:
>
> 1. Run SSMA in a Windows VM (Hyper-V, VMware Fusion, Parallels) or a
>    Windows-based jump host.
> 2. Use Windows Server in Azure with the SSMA installer.
> 3. Skip Pipeline A and rely on Pipeline B (Ora2Pg) toward PostgreSQL only,
>    accepting that you lose the cross-converter sanity check toward the
>    SQL-Server-family target.
>
> The chapter assumes one of these paths is available. Don't fight the tool.

## Install (Windows)

1. Download the SSMA for Oracle installer:
   <https://learn.microsoft.com/sql/ssma/oracle/installing-ssma-for-oracle>
2. Install. Required dependencies: .NET Framework 4.7.2+ and the Oracle Data
   Access Components (ODAC) — both fetched by the installer when missing.
3. Default install path:
   `C:\Program Files\Microsoft SQL Server Migration Assistant for Oracle\`
4. Verify the console binary:
   `Bin\SSMAforOracleConsole.exe -?`

## Run

```powershell
# From a PowerShell prompt in this directory:
..\..\scripts\02_run_ssma.ps1
```

The wrapper:

1. Validates the variables file has no `REPLACE_WITH_*` placeholders left.
2. Invokes `SSMAforOracleConsole.exe -s ssma_project.scscript -v ssma_variables.xml`.
3. Copies the generated SQL and the assessment HTML report to
   `converted/ssma/` so `03_conversion_diff.py` can pick them up.

## What SSMA produces

- An **assessment report** (`reports/AssessmentReport.html`) that classifies
  every source object as fully convertible, convertible-with-warnings, or
  not-convertible.
- **T-SQL DDL** scripts under the project folder, organized per object type.
- A **synchronization log** capturing what was applied to the target Azure
  SQL Database.

The book's `03_conversion_diff.py` script scans the T-SQL output (not the
HTML) so the per-tool decision surface ends up in the same machine-readable
`car.json` as Ora2Pg's output.
