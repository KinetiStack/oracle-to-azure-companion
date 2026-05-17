// =============================================================================
// HrProClient.java -- SOURCE version (talks to Oracle 19c HR-Pro).
//
// This is the canonical "before" shape for the chapter. The MI and PG
// directories under this tree contain the same class refactored for the two
// Azure target families, with every line that changed annotated.
//
// Build:  javac -cp ojdbc11.jar HrProClient.java
// Run :   java  -cp .:ojdbc11.jar HrProClient
// =============================================================================
import java.sql.*;
import java.util.Properties;

public class HrProClient {

    // -- (a) JDBC URL: Oracle TNS-style. ----------------------------------------
    //
    // EZConnect single-host form:
    //   jdbc:oracle:thin:@//host:1521/ORCLPDB1
    // Full descriptor for RAC SCAN + TAF (Transparent Application Failover):
    private static final String JDBC_URL =
        "jdbc:oracle:thin:@(DESCRIPTION="
      +   "(ADDRESS_LIST="
      +     "(LOAD_BALANCE=ON)"
      +     "(ADDRESS=(PROTOCOL=TCP)(HOST=prod-rac-scan)(PORT=1521))"
      +   ")"
      +   "(CONNECT_DATA=(SERVICE_NAME=ORA19CPROD)"
      +     "(FAILOVER_MODE=(TYPE=SELECT)(METHOD=BASIC)(RETRIES=10)(DELAY=2))"
      +   ")"
      + ")";

    // -- (b) Oracle JDBC driver class. -------------------------------------------
    private static final String DRIVER_CLASS = "oracle.jdbc.OracleDriver";

    public static void main(String[] args) throws Exception {
        Class.forName(DRIVER_CLASS);

        Properties props = new Properties();
        props.setProperty("user",     System.getenv("HRPRO_USER"));
        props.setProperty("password", System.getenv("HRPRO_PWD"));

        try (Connection conn = DriverManager.getConnection(JDBC_URL, props)) {
            conn.setAutoCommit(false);

            // -- (c) Oracle-isms in the SQL: ROWNUM, SYSDATE, NVL, FROM DUAL,
            //        SEQUENCE.NEXTVAL. Each of these has to change for the
            //        target engine; the analyzer (§ 8.5.2) flags them.
            String sql =
                "SELECT * FROM ("
              +   "SELECT e.emp_id, e.first_name, e.last_name, "
              +          "NVL(eh.salary, 0) AS salary, "
              +          "SYSDATE AS as_of "
              +     "FROM hrpro.employee e "
              +     "LEFT JOIN hrpro.employee_history eh "
              +            "ON eh.emp_id = e.emp_id AND eh.end_date IS NULL "
              +    "ORDER BY e.emp_id"
              + ") WHERE ROWNUM <= 10";

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

            // Insert via sequence
            try (PreparedStatement ps = conn.prepareStatement(
                    "INSERT INTO hrpro.payroll_run(run_id, run_date, status, started_at) "
                  + "VALUES (hrpro.seq_payroll_run.NEXTVAL, SYSDATE, 'PROBE', SYSTIMESTAMP)")) {
                ps.executeUpdate();
            }
            conn.commit();
        }
    }
}
