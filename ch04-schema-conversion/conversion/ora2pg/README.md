# Ora2Pg — Pipeline B (Oracle → Azure DB for PostgreSQL Flex)

Ora2Pg is a Perl tool that converts Oracle schemas and PL/SQL into PostgreSQL
DDL and PL/pgSQL. Cross-platform (Linux, macOS, Windows). This README covers
installation and lab use; the book's chapter prose has the full pedagogical
walkthrough.

## Install (Linux / macOS)

Ora2Pg needs Perl, the DBI module, and `DBD::Oracle`. `DBD::Oracle` requires
Oracle Instant Client headers and libraries.

```bash
# Debian / Ubuntu
sudo apt-get install -y perl libdbi-perl libdbd-oracle-perl ora2pg

# RHEL / Fedora
sudo dnf install -y perl perl-DBI perl-DBD-Oracle ora2pg

# macOS (Homebrew has no ora2pg formula; install from CPAN)
brew install perl
curl -fsSL https://github.com/darold/ora2pg/archive/refs/tags/v24.3.tar.gz | tar xz
cd ora2pg-24.3 && perl Makefile.PL && make && sudo make install
```

If `DBD::Oracle` fails to build (the most common installation friction on
macOS), download the Oracle Instant Client Basic + SDK packages from Oracle's
site, point `ORACLE_HOME` at the extracted directory, and re-run `cpan
DBD::Oracle`.

## Run

Edit `ora2pg.conf` and replace the `__SET_BY_WRAPPER__` placeholders with your
actual values, or use the wrapper script which substitutes them at runtime:

```bash
bash ../scripts/02_run_ora2pg.sh
```

Output appears under `./converted/ora2pg/`.

## Per-type passes

`ora2pg.conf` ships with `TYPE=TABLE`. The wrapper runs additional passes by
overriding `-t` on the command line:

```bash
ora2pg -c ora2pg.conf -t TYPE      -o hr_pro_types.sql
ora2pg -c ora2pg.conf -t TABLE     -o hr_pro_tables.sql
ora2pg -c ora2pg.conf -t INDEXES   -o hr_pro_indexes.sql
ora2pg -c ora2pg.conf -t SEQUENCE  -o hr_pro_sequences.sql
ora2pg -c ora2pg.conf -t TRIGGER   -o hr_pro_triggers.sql
ora2pg -c ora2pg.conf -t MVIEW     -o hr_pro_mviews.sql
ora2pg -c ora2pg.conf -t PACKAGE   -o hr_pro_packages.sql
```

Splitting by type keeps a parse failure in one type from masking output in
another. The wrapper handles this loop.
