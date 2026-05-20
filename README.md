# oracle-to-azure-companion

Sample companion code for **Migrating Oracle Databases to Azure Cloud** (KinetiStack Press, 2026) — the typed, signed-deliverable playbook for the enterprise-scale Oracle 19c → Azure migration.

This is the **public** teaser repository. It ships **Chapter 1 — Automated Discovery and Schema Assessment** as a runnable sample so prospective readers can see the editorial Code Truth Principle in action: every script printed in the book is real, runnable, and verifiable.

The remaining 14 chapters' code is in the **premium** companion repo, gated to verified book purchasers.

## What's here

```
ch01-discovery-assessment/   Oracle source assessment (Migration Assessment Bundle)
└── scripts/                 SQL + Python for feature usage audit, schema complexity,
                             workload baseline, MAB JSON producer
```

Try it:

```bash
cd ch01-discovery-assessment/scripts
# Run against an Oracle source — produces the Migration Assessment Bundle (MAB)
./produce_mab.py --target-host <oracle-host> --service-name <svc>
```

The MAB is the input contract for Chapter 2's Target Architecture Selection Matrix (TASM) — see the full chapter chain in the book.

## Full companion code (premium)

The private companion repo `oracle-to-azure-premium` contains:

| Chapter | Artifact |
|---|---|
| 2 — Target Selection | TASM evaluator + JSON schema |
| 3 — TCO Sizing | BoM generator + Azure pricing model |
| 3.5 — Lab Setup | Reproducible lab (docker-compose, Bicep) |
| 4 — Schema Conversion | SSMA / Ora2Pg drivers + CAR aggregator |
| 5 — PL/SQL Refactoring | T-SQL + PL/pgSQL refactor patterns |
| 6 — Offline Migration | Data Pump + Data Box pipeline (MRR) |
| 7 — Online Replication | GoldenGate + LSR tooling |
| 8 — Network Security | Hub-Spoke Bicep + AMR pipeline |
| 8.5 — App-Tier Refactor | JDBC/Hibernate + ARR |
| 9 — Databricks Offload | Medallion ORR classifier |
| 10 — HA/DR | Data Guard + MI failover + PG replica + HDR drill |
| 11 — Performance Tuning | PVR golden-query runner |
| 12 — Cutover | CRR aggregator + cutover runbook |
| 13 — Decommissioning | Decom timeline + DRR cost-attribution |

Plus:

- **Full HR-Pro lab dataset** — 35,000+ rows seeded across 6 tables (this teaser ships the minimal lab; premium ships the production-equivalent)
- **Production-grade Bicep modules** — fully parameterized with environment files, Pulumi/Terraform equivalents
- **Reference engagement artifacts** — signed example MAB/TASM/BoM/…/DRR JSONs from anonymized real engagements
- **v1.1+ updates** — chapter addenda as Azure GA cycles and Oracle Database@Azure region expansion happen

**Claim premium access** (free for verified book purchasers, regardless of channel):
**<https://kinetistack.co/access>**

## How to verify a script is genuine

Every script here matches verbatim what is printed in Chapter 1 of the book. If you find a divergence between this repo and the printed page, that is a defect — please report it.

## License

MIT for the code in this repository. See [`LICENSE`](./LICENSE). The license applies to the **code only**. The book text, diagrams, and editorial content are copyright © 2026 KinetiStack LLC, all rights reserved.

## Errata + defects

If a script doesn't run, a JSON Schema doesn't validate, or example output doesn't match what the page shows, that is a defect. Open an issue or email `publishing@kinetistack.co`.

---

*KinetiStack Press · publishing@kinetistack.co · [Buy the book](https://leanpub.com/oracle-to-azure)*
