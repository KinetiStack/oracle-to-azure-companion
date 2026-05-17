// =============================================================================
// HrProClient.java -- TARGET version for Azure SQL Database / Azure SQL MI.
//
// Diff from ../oracle/HrProClient.java is intentional and small. Every line
// that changed is marked with `// CHANGED:`.
//
// Build:  javac -cp mssql-jdbc-12.x.x.jre11.jar HrProClient.java
// Run :   java  -cp .:mssql-jdbc-12.x.x.jre11.jar HrProClient
//
// Dependencies: Microsoft JDBC Driver for SQL Server 12.x+ (supports Azure
// SQL DB and SQL MI; required for AAD authentication and modern TLS).
// =============================================================================
import java.sql.*;
import java.util.Properties;

public class HrProClient {

    // CHANGED: JDBC URL is sqlserver://, with the PEP'd hostname and
    //          encrypt=true (mandatory for Azure SQL endpoints).
    private static final String JDBC_URL =
        "jdbc:sqlserver://oramigsqlsrvXXX.database.windows.net:1433"
      + ";database=labdb"
      + ";encrypt=true"
      + ";trustServerCertificate=false"
      + ";hostNameInCertificate=*.database.windows.net"
      + ";loginTimeout=30";

    // CHANGED: driver class.
    private static final String DRIVER_CLASS = "com.microsoft.sqlserver.jdbc.SQLServerDriver";

    public static void main(String[] args) throws Exception {
        Class.forName(DRIVER_CLASS);

        Properties props = new Properties();
        props.setProperty("user",     System.getenv("HRPRO_USER"));
        props.setProperty("password", System.getenv("HRPRO_PWD"));

        // CHANGED: wrap connection acquisition in retry. Azure SQL emits
        //          transient errors (40197, 40501, 40613, ...) that the
        //          old Oracle JDBC driver handled via TAF; we handle them
        //          in application code now. See ../retry/AzureSqlRetry.java.
        try (Connection conn = AzureSqlRetry.openConnection(JDBC_URL, props)) {
            conn.setAutoCommit(false);

            // CHANGED: T-SQL flavor.
            //   ROWNUM <= 10      -> TOP (10)
            //   NVL(x, 0)         -> COALESCE(x, 0)
            //   SYSDATE           -> GETUTCDATE() (or SYSUTCDATETIME())
            //   FROM DUAL         -> (removed)
            //   seq.NEXTVAL       -> NEXT VALUE FOR seq
            String sql =
                "SELECT TOP (10) e.emp_id, e.first_name, e.last_name, "
              +        "COALESCE(eh.salary, 0) AS salary, "
              +        "GETUTCDATE() AS as_of "
              +   "FROM dbo.employee AS e "
              +   "LEFT JOIN dbo.employee_history AS eh "
              +          "ON eh.emp_id = e.emp_id AND eh.end_date IS NULL "
              +  "ORDER BY e.emp_id";

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
                    "INSERT INTO dbo.payroll_run(run_id, run_date, status, started_at) "
                  + "VALUES (NEXT VALUE FOR dbo.seq_payroll_run, "
                  +         "CAST(GETUTCDATE() AS DATE), 'PROBE', SYSUTCDATETIME())")) {
                ps.executeUpdate();
            }
            conn.commit();
        }
    }
}
