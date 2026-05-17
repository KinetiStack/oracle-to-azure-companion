#!/usr/bin/env python3
"""scan_app.py - scan an application codebase for Oracle-specific patterns
that must be refactored for an Azure SQL or PostgreSQL target.

Emits an Application Refactor Report (ARR) JSON document conforming to
arr_schema.json. Consumed by the migration backlog.

Scope:
  - Java, Python, C#, JavaScript/TypeScript source files
  - Spring application.properties / .yml
  - Hibernate cfg.xml, persistence.xml
  - Anything else with a documented Oracle-isms regex match

What it does NOT do:
  - Parse SQL into an AST (false positives in comments / strings are accepted)
  - Modify code -- it only reports

Usage:
    scan_app.py --root /path/to/repo --out arr.json
"""
from __future__ import annotations

import argparse
import dataclasses
import json
import logging
import re
import sys
from datetime import datetime, timezone
from pathlib import Path
from typing import Any

ARR_VERSION = "1.0.0"
log = logging.getLogger("scan_app")

# ---------------------------------------------------------------------------
# Pattern catalog. Each entry maps a regex to a remediation hint. Keep the
# patterns conservative -- the goal is "obvious migration targets," not a
# full SQL parser. False positives on identifiers like `mySYSDATEColumn`
# are acceptable; the reviewer reads the flagged file anyway.
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class Pattern:
    name:        str
    regex:       re.Pattern
    severity:    str
    remediation: str


PATTERNS: tuple[Pattern, ...] = (
    Pattern(
        "oracle_jdbc_url",
        re.compile(r"jdbc:oracle:thin:@"),
        "HIGH",
        "Replace with jdbc:sqlserver:// (MI) or jdbc:postgresql:// (PG). "
        "See § 8.5.3.",
    ),
    Pattern(
        "oracle_driver_class",
        re.compile(r"\boracle\.jdbc\.OracleDriver\b"),
        "HIGH",
        "Replace with com.microsoft.sqlserver.jdbc.SQLServerDriver or org.postgresql.Driver.",
    ),
    Pattern(
        "hibernate_oracle_dialect",
        re.compile(r"\borg\.hibernate\.dialect\.Oracle\w*Dialect\b"),
        "HIGH",
        "Switch to SQLServer2019Dialect (MI) or PostgreSQLDialect (PG). "
        "Set explicitly; do NOT rely on autodetect (see § 8.5.5 / P17).",
    ),
    Pattern(
        "tns_descriptor",
        re.compile(r"\(\s*DESCRIPTION\s*=\s*\(\s*ADDRESS", re.IGNORECASE),
        "MEDIUM",
        "TNS descriptor implies RAC + TAF. Replace with single-host JDBC URL "
        "and application-layer retry (§ 8.5.6).",
    ),
    Pattern(
        "oracle_failover_mode",
        re.compile(r"\bFAILOVER_MODE\s*=", re.IGNORECASE),
        "MEDIUM",
        "Oracle TAF clause. No driver-level equivalent on Azure; replace "
        "with application-layer retry (resilience4j / tenacity / Polly).",
    ),
    Pattern(
        "rownum",
        re.compile(r"\bROWNUM\b", re.IGNORECASE),
        "MEDIUM",
        "Replace with TOP N or OFFSET ... FETCH NEXT (T-SQL) or LIMIT N (PG).",
    ),
    Pattern(
        "nvl",
        re.compile(r"\bNVL\s*\(", re.IGNORECASE),
        "LOW",
        "Replace with COALESCE() -- ANSI-standard; works on all three engines.",
    ),
    Pattern(
        "sysdate",
        re.compile(r"\bSYSDATE\b", re.IGNORECASE),
        "LOW",
        "Replace with CURRENT_TIMESTAMP (ANSI) or engine-specific "
        "(GETUTCDATE / SYSUTCDATETIME on T-SQL; clock_timestamp on PG).",
    ),
    Pattern(
        "from_dual",
        re.compile(r"\bFROM\s+DUAL\b", re.IGNORECASE),
        "LOW",
        "Remove the FROM DUAL clause -- SQL Server and PG don't require it.",
    ),
    Pattern(
        "sequence_nextval",
        re.compile(r"\.NEXTVAL\b", re.IGNORECASE),
        "MEDIUM",
        "Oracle seq.NEXTVAL -> MI: NEXT VALUE FOR seq; PG: nextval('seq').",
    ),
    Pattern(
        "utl_file",
        re.compile(r"\bUTL_FILE\b", re.IGNORECASE),
        "HIGH",
        "Replace with the file-emission queue pattern from Ch.5 § 5.3.",
    ),
    Pattern(
        "dbms_pipe",
        re.compile(r"\bDBMS_PIPE\b", re.IGNORECASE),
        "HIGH",
        "No equivalent on Azure targets. Move to Service Bus / Event Grid.",
    ),
    Pattern(
        "rownum_alt",
        re.compile(r"\bMINUS\b", re.IGNORECASE),
        "LOW",
        "Oracle MINUS -> EXCEPT (ANSI, supported on T-SQL and PG).",
    ),
)

# File extensions to scan
EXTENSIONS: tuple[str, ...] = (
    ".java", ".py", ".cs", ".js", ".ts", ".jsx", ".tsx",
    ".kt", ".scala", ".groovy",
    ".sql", ".properties", ".yml", ".yaml", ".xml", ".json",
    ".cfg", ".ini", ".conf",
)


@dataclasses.dataclass
class Finding:
    file:           str
    line:           int
    pattern:        str
    severity:       str
    matched_text:   str
    remediation:    str


# ---------------------------------------------------------------------------
def scan_file(path: Path, root: Path) -> list[Finding]:
    rel = str(path.relative_to(root))
    findings: list[Finding] = []
    try:
        text = path.read_text(encoding="utf-8", errors="replace")
    except OSError as exc:
        log.warning("could not read %s: %s", rel, exc)
        return findings

    for ln, line in enumerate(text.splitlines(), start=1):
        for pat in PATTERNS:
            m = pat.regex.search(line)
            if m:
                findings.append(Finding(
                    file=rel, line=ln, pattern=pat.name,
                    severity=pat.severity, matched_text=m.group(0),
                    remediation=pat.remediation,
                ))
    return findings


def scan_tree(root: Path) -> list[Finding]:
    findings: list[Finding] = []
    for path in sorted(root.rglob("*")):
        if not path.is_file():
            continue
        if path.suffix.lower() not in EXTENSIONS:
            continue
        # Skip likely build / vendored directories.
        rel_parts = path.relative_to(root).parts
        if any(p in {"node_modules", "target", "build", "dist", ".git", "__pycache__"}
               for p in rel_parts):
            continue
        findings.extend(scan_file(path, root))
    return findings


def build_arr(findings: list[Finding], root: Path) -> dict[str, Any]:
    by_severity: dict[str, int] = {"HIGH": 0, "MEDIUM": 0, "LOW": 0}
    by_pattern:  dict[str, int] = {}
    files_with_findings: set[str] = set()
    for f in findings:
        by_severity[f.severity] = by_severity.get(f.severity, 0) + 1
        by_pattern[f.pattern]   = by_pattern.get(f.pattern, 0) + 1
        files_with_findings.add(f.file)

    return {
        "_artifact":         "arr",
        "root_scanned":      str(root),
        "generated_at_utc":  datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "arr_version":       ARR_VERSION,
        "summary": {
            "total_findings":      len(findings),
            "files_with_findings": len(files_with_findings),
            "by_severity":         by_severity,
            "by_pattern":          dict(sorted(by_pattern.items(),
                                               key=lambda kv: -kv[1])),
        },
        "findings": [dataclasses.asdict(f) for f in findings],
        "open_questions": [],
    }


def main(argv: list[str] | None = None) -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--root", required=True, type=Path,
                   help="Root directory of the application codebase to scan.")
    p.add_argument("--out",  required=True, type=Path,
                   help="Output path for arr.json.")
    p.add_argument("--verbose", "-v", action="store_true")
    args = p.parse_args(argv)

    logging.basicConfig(
        level=logging.DEBUG if args.verbose else logging.INFO,
        format="%(asctime)s %(levelname)s %(name)s: %(message)s",
    )

    if not args.root.is_dir():
        p.error(f"--root path is not a directory: {args.root}")

    findings = scan_tree(args.root)
    arr = build_arr(findings, args.root)

    args.out.parent.mkdir(parents=True, exist_ok=True)
    args.out.write_text(
        json.dumps(arr, indent=2, sort_keys=True) + "\n",
        encoding="utf-8",
    )
    s = arr["summary"]
    log.info("ARR written: %s", args.out)
    log.info("Files scanned with findings: %d  Total findings: %d  "
             "(HIGH=%d MEDIUM=%d LOW=%d)",
             s["files_with_findings"], s["total_findings"],
             s["by_severity"]["HIGH"], s["by_severity"]["MEDIUM"],
             s["by_severity"]["LOW"])
    # Exit non-zero if HIGH-severity findings exist (CI gate hook).
    return 0 if s["by_severity"]["HIGH"] == 0 else 1


if __name__ == "__main__":
    sys.exit(main())
