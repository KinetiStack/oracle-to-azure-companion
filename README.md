# oracle-to-azure-companion

Companion code for **Migrating Oracle Databases to Azure Cloud** (KinetiStack Press, 2026) — the typed, signed-deliverable playbook for the enterprise-scale Oracle 19c → Azure migration.

This is the **public** companion repository. Every script printed in the book is here, verbatim, so readers can copy-paste and run. The book's editorial Code Truth Principle requires every example to be runnable; this repository is how that promise is kept.

For **premium content** — full lab data, production-grade Bicep modules, walkthrough recordings, and future-edition updates — see the gated companion at `oracle-to-azure-premium`. Access is granted to verified book purchasers via the claim portal at <https://kinetistack.co/access>.

## Repository layout

```
.
├── ch01-discovery-assessment/      Oracle source assessment (MAB)
│   └── scripts/
├── ch02-target-selection/          Target architecture matrix (TASM)
│   └── scripts/
├── ch03-tco-sizing/                BoM generator + Azure pricing model
│   └── scripts/
├── ch03-5-lab-setup/               Reproducible lab (docker-compose, Bicep)
├── ch04-schema-conversion/         SSMA / Ora2Pg drivers + CAR aggregator
│   └── scripts/
├── ch05-plsql-refactoring/         T-SQL + PL/pgSQL refactor patterns
│   └── refactor/
├── ch06-offline-migration/         Data Pump + Data Box pipeline (MRR)
│   └── scripts/
├── ch07-online-replication/        GoldenGate + LSR tooling
├── ch08-network-security/          Hub-Spoke Bicep + AMR pipeline
│   └── bicep/
├── ch08-5-app-tier-refactor/       JDBC/Hibernate + ARR
├── ch09-databricks-offload/        Medallion ORR classifier
├── ch10-hadr/                      Data Guard + MI failover + PG replica + HDR drill
│   └── scripts/
├── ch11-perf-tuning/               PVR golden-query runner
├── ch12-cutover/                   CRR aggregator + runbook
└── ch13-decom-cost/                Decom timeline + DRR
```

Each chapter directory mirrors the section in the book. The `README.md` inside each chapter folder names the artifact the chapter produces and gives one-line pointers to the relevant scripts.

## How to use

1. **Read the book first.** Every script assumes the conceptual model (the data chain: MAB → TASM → BoM → … → DRR) introduced in Chapter 1. Running scripts without the book context will work but you will miss the *why*.
2. **Stand up the Chapter 3.5 lab.** The lab is the prerequisite for every later chapter. One command:
   ```bash
   cd ch03-5-lab-setup && docker compose up -d
   ```
3. **Run scripts in chapter order.** Each chapter's artifact is the input contract for the next chapter. CI exit codes gate the chain.
4. **Validate against the JSON Schemas** shipped alongside each artifact (e.g., `ch01-discovery-assessment/scripts/mab_schema.json`).

## License

MIT. See [`LICENSE`](./LICENSE). The license applies to the **code only**. The book text, diagrams, and editorial content are copyright © 2026 KinetiStack LLC, all rights reserved.

## Errata + defects

If a script doesn't run, a schema doesn't validate, or example output doesn't match what the page shows, that is a defect. Open an issue on this repository or email `publishing@kinetistack.co`. The book's editorial discipline treats every such report as a tracked defect for the next edition.

## Premium content

The private companion repo `oracle-to-azure-premium` adds:

- **Full HR-Pro lab dataset** — 35,000+ rows seeded across `employee`, `employee_history`, `payroll_run`, `dept`, `audit_log`, `mv_headcount_rollup`. The book ships a minimal lab; premium ships the production-equivalent.
- **Production-grade Bicep modules** — fully parameterized, with `main.bicep` orchestration, parameter files per environment, and Pulumi/Terraform equivalents.
- **Walkthrough recordings** — screen-recorded chapter walkthroughs for the lab + key cutover scenarios.
- **Reference engagement artifacts** — signed example MAB/TASM/BoM/.../DRR JSONs from an anonymized real engagement, with field-level annotations.
- **v1.1+ updates** — chapter addenda and corrections as Azure GA cycles and Oracle Database@Azure region expansion happen.

Claim premium access: <https://kinetistack.co/access> (free for verified book purchasers, regardless of channel).

---

*KinetiStack Press · publishing@kinetistack.co*
