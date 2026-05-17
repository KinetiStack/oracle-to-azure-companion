// =============================================================================
// HrProClient.java -- TARGET version for Azure DB for PostgreSQL Flex.
//
// Diff from ../oracle/HrProClient.java is intentional and small. Every line
// that changed is marked with `// CHANGED:`.
//
// Build:  javac -cp postgresql-42.7.x.jar HrProClient.java
// Run :   java  -cp .:postgresql-42.7.x.jar HrProClient
//
// Dependencies: pgjdbc 42.7+. Earlier versions lack the cleanest sslmode
// defaults and OAuth token-based auth.
// =============================================================================
import java.sql.*;
import java.util.Properties;

public class HrProClient {

    // CHANGED: JDBC URL is jdbc:postgresql://, with sslmode=require
    //          (Azure DB for PG Flex rejects non-SSL connections).
    private static final String JDBC_URL =
        "jdbc:postgresql://oramigpgXXX.postgres.database.azure.com:5432/labdb"
      + "?sslmode=require"
      + "&sslrootcert=/etc/ssl/certs/DigiCertGlobalRootCA.crt.pem"
      + "&connectTimeout=30";

    // CHANGED: driver class.
    private static final String DRIVER_CLASS = "org.postgresql.Driver";

    public static void main(String[] args) throws Exception {
        Class.forName(DRIVER_CLASS);

        Properties props = new Properties();
        props.setProperty("user",     System.getenv("HRPRO_USER"));
        props.setProperty("password", System.getenv("HRPRO_PWD"));

        // CHANGED: application-layer retry; see ../retry/pg_retry.py for the
        //          Python equivalent. Java apps typically use resilience4j;
        //          this snippet shows the connect-level retry, query-level
        //          retry is covered in the chapter prose.
        try (Connection conn = PgRetry.openConnection(JDBC_URL, props)) {
            conn.setAutoCommit(false);

            // CHANGED: PG flavor.
            //   ROWNUM <= 10      -> LIMIT 10
            //   NVL(x, 0)         -> COALESCE(x, 0)
            //   SYSDATE           -> CURRENT_TIMESTAMP (or clock_timestamp())
            //   FROM DUAL         -> (removed)
            //   seq.NEXTVAL       -> nextval('seq')
            //   schema case       -> lowercase (PG case-folds unquoted identifiers)
            String sql =
                "SELECT e.emp_id, e.first_name, e.last_name, "
              +        "COALESCE(eh.salary, 0) AS salary, "
              +        "clock_timestamp() AS as_of "
              +   "FROM hrpro.employee e "
              +   "LEFT JOIN hrpro.employee_history eh "
              +          "ON eh.emp_id = e.emp_id AND eh.end_date IS NULL "
              +  "ORDER BY e.emp_id "
              +  "LIMIT 10";

            try (PreparedStatement ps = conn.prepareStatement(sql);
                 ResultSet rs = ps.executeQuery()) {
                while (rs.next()) {
                    System.out.printf("%d | %s %s | %.2f | %s%n",
                        rs.getLong("emp_id"),
                        rs.getString("first_name"),
                        rs.getString("last_name"),
                        rs.getBigDecimal("salary"),
                        rs.getTimestamp("as_of"));
                }
            }

            try (PreparedStatement ps = conn.prepareStatement(
                    "INSERT INTO hrpro.payroll_run(run_id, run_date, status, started_at) "
                  + "VALUES (nextval('hrpro.seq_payroll_run'), "
                  +         "CURRENT_DATE, 'PROBE', clock_timestamp())")) {
                ps.executeUpdate();
            }
            conn.commit();
        }
    }
}
