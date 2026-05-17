// =============================================================================
// AzureSqlRetry.java -- application-layer retry for Azure SQL DB / SQL MI.
//
// Replaces Oracle RAC TAF (Transparent Application Failover) which auto-
// retries inside the Oracle JDBC driver. Azure SQL has no driver-side TAF
// equivalent; we wrap connection acquisition and query execution in a
// retry policy that recognizes Azure SQL's documented transient error codes.
//
// Dependencies: resilience4j-retry 2.x, microsoft-jdbc-driver 12.x.
// Production should also wire resilience4j-micrometer for metrics.
// =============================================================================
import io.github.resilience4j.retry.Retry;
import io.github.resilience4j.retry.RetryConfig;
import io.github.resilience4j.core.IntervalFunction;

import java.sql.*;
import java.time.Duration;
import java.util.Properties;
import java.util.Set;
import java.util.function.Function;

public final class AzureSqlRetry {

    // Azure SQL Database / SQL MI transient error codes per Microsoft's
    // "Troubleshoot transient connection errors in Azure SQL" doc. Update
    // periodically; new codes are added when Microsoft revises the doc.
    private static final Set<Integer> TRANSIENT_CODES = Set.of(
        4060,    // login refused (under-provisioned tier)
        40197,   // service error during reconfiguration
        40501,   // service is busy
        40613,   // database is unavailable
        49918, 49919, 49920,   // session limit reached
        11001,   // host unreachable (transient DNS)
        10928,   // resource ID limit exceeded
        10929    // resource governor min not honored
    );

    private static final Retry RETRY = Retry.of("azure-sql", RetryConfig.custom()
        .maxAttempts(5)
        .intervalFunction(IntervalFunction.ofExponentialRandomBackoff(
            /* initial */ Duration.ofMillis(200),
            /* multiplier */ 2.0,
            /* randomization */ 0.5,
            /* max */ Duration.ofSeconds(10)))
        .retryOnException(AzureSqlRetry::isTransient)
        .build());

    private AzureSqlRetry() {}

    public static boolean isTransient(Throwable t) {
        if (!(t instanceof SQLException)) return false;
        SQLException e = (SQLException) t;
        if (TRANSIENT_CODES.contains(e.getErrorCode())) return true;
        // Some transient errors arrive without a numeric code; the driver
        // reports them as ConnectionReset / "forcibly closed". Inspect the
        // message as a last resort.
        String msg = e.getMessage();
        return msg != null && (
            msg.contains("connection was forcibly closed")
         || msg.contains("connection has been closed")
         || msg.contains("transport-level error"));
    }

    public static Connection openConnection(String url, Properties props) throws SQLException {
        return wrap(() -> DriverManager.getConnection(url, props));
    }

    public static <T> T wrap(SqlSupplier<T> op) throws SQLException {
        try {
            return Retry.decorateCallable(RETRY, op::get).call();
        } catch (SQLException sql) {
            throw sql;
        } catch (Exception e) {
            throw new SQLException(e);
        }
    }

    @FunctionalInterface
    public interface SqlSupplier<T> {
        T get() throws SQLException;
    }
}
