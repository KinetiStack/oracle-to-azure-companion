#!/usr/bin/env python3
"""hr_pro_seed.py - deterministic synthetic data loader for the HR-Pro lab.

Inserts ~5,000 employees and ~30,000 employee_history rows into the lab's
Oracle 19c source. Uses python-oracledb in thin mode so no Oracle Instant
Client install is required on the host.

Run time: ~30 seconds on a laptop.

Usage:
    python3 hr_pro_seed.py --user hrpro --password '...' \\
                           --dsn 'localhost:1521/ORCLPDB1' [--seed 42]
"""
from __future__ import annotations
import argparse
import logging
import random
import sys
from datetime import date, timedelta

import oracledb

EMP_COUNT  = 5_000
HIST_COUNT = 30_000

DEPARTMENTS: list[tuple[int, str, str, str]] = [
    (1, "ENG",   "Engineering",            "CC-1001"),
    (2, "OPS",   "Operations",             "CC-1002"),
    (3, "FIN",   "Finance",                "CC-1003"),
    (4, "SALES", "Sales & Marketing",      "CC-1004"),
]

GRADES: list[tuple[int, str, int, int]] = [
    (1, "L3",  60_000,  90_000),
    (2, "L4",  85_000, 120_000),
    (3, "L5", 110_000, 160_000),
    (4, "L6", 140_000, 210_000),
    (5, "L7", 180_000, 280_000),
]

FIRST = ["Avery","Bailey","Casey","Drew","Emerson","Finley","Greer","Harper",
        "Ira","Jules","Kai","Logan","Morgan","Noor","Oakley","Parker","Quinn",
        "Reese","Sage","Taylor","Uri","Vega","Wren","Xen","Yael","Zion"]
LAST  = ["Adler","Bryant","Chen","Diaz","Eriksen","Fontaine","Garza","Hassan",
        "Imai","Jovic","Kapoor","Liang","Moreno","Nakamura","OConnor","Park",
        "Quan","Rao","Singh","Tanaka","Underhill","Vargas","Wei","Xu","Young","Zoric"]


def main() -> int:
    p = argparse.ArgumentParser(description=__doc__.splitlines()[0])
    p.add_argument("--user",     required=True)
    p.add_argument("--password", required=True)
    p.add_argument("--dsn",      required=True, help="e.g. localhost:1521/ORCLPDB1")
    p.add_argument("--seed",     type=int, default=42)
    args = p.parse_args()

    logging.basicConfig(level=logging.INFO,
                        format="%(asctime)s %(levelname)s: %(message)s")
    rnd = random.Random(args.seed)

    logging.info("Connecting (thin mode) to %s", args.dsn)
    with oracledb.connect(user=args.user, password=args.password, dsn=args.dsn) as conn:
        cur = conn.cursor()

        logging.info("Seeding reference tables")
        cur.executemany(
            "INSERT INTO department(dept_id, dept_code, dept_name, cost_center) "
            "VALUES (:1, :2, :3, :4)", DEPARTMENTS)
        cur.executemany(
            "INSERT INTO job_grade(grade_id, grade_code, min_salary, max_salary) "
            "VALUES (:1, :2, :3, :4)", GRADES)
        conn.commit()

        logging.info("Inserting %d employees", EMP_COUNT)
        emp_rows = []
        for i in range(1, EMP_COUNT + 1):
            fn = rnd.choice(FIRST)
            ln = rnd.choice(LAST)
            ssn = f"{rnd.randint(100,999)}-{rnd.randint(10,99)}-{rnd.randint(1000,9999)}"
            hire = date(rnd.randint(2018, 2025), rnd.randint(1, 12), rnd.randint(1, 28))
            dept_id  = rnd.choice(DEPARTMENTS)[0]
            grade_id = rnd.choice(GRADES)[0]
            email    = f"{fn.lower()}.{ln.lower()}{i}@example.com"
            emp_rows.append((i, f"E{i:06d}", fn, ln, ssn, email, hire, dept_id, grade_id))

        cur.executemany(
            "INSERT INTO employee(emp_id, emp_number, first_name, last_name, ssn, "
            "email, hire_date, dept_id, grade_id) "
            "VALUES (:1, :2, :3, :4, :5, :6, :7, :8, :9)", emp_rows)
        conn.commit()

        logging.info("Inserting %d employee_history rows", HIST_COUNT)
        hist_rows = []
        for i in range(1, HIST_COUNT + 1):
            emp_id   = rnd.randint(1, EMP_COUNT)
            grade    = rnd.choice(GRADES)
            eff      = date(rnd.randint(2022, 2026), rnd.randint(1, 12), rnd.randint(1, 28))
            end      = None if rnd.random() < 0.3 else (eff + timedelta(days=rnd.randint(30, 800)))
            salary   = rnd.randint(grade[2], grade[3])
            hist_rows.append((i, emp_id, eff, end,
                              rnd.choice(DEPARTMENTS)[0], grade[0], salary, "Lab seed"))

        cur.executemany(
            "INSERT INTO employee_history(history_id, emp_id, effective_date, end_date, "
            "dept_id, grade_id, salary, change_reason) "
            "VALUES (:1, :2, :3, :4, :5, :6, :7, :8)", hist_rows)
        conn.commit()

        logging.info("Refreshing materialized view")
        cur.callproc("DBMS_MVIEW.REFRESH", ["MV_HEADCOUNT_ROLLUP", "C"])

        logging.info("Seed complete.")
    return 0


if __name__ == "__main__":
    sys.exit(main())
