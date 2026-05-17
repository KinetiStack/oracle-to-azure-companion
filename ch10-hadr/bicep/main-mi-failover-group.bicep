// =============================================================================
// Azure SQL Managed Instance auto-failover group across two regions.
//
// Correct ARM resource type for MI failover groups is:
//   Microsoft.Sql/locations/instanceFailoverGroups
// (a CHILD of the location pseudo-resource, NOT of the MI itself). The
// resource is deployed at the resource-group scope of the PRIMARY MI; the
// region is encoded in the child resource name as `${primaryRegion}/${fog-name}`.
//
// MI failover group differs from SQL DB failover group in subtle ways:
//   - Operates at the INSTANCE level (every database fails over together).
//   - Listener endpoints (read/write + read-only) follow the active role.
//   - Cross-region failover groups REQUIRE the secondary MI in a region
//     from the documented MI failover-group pair list.
//
// Cost: each MI replica costs the full vCore + storage of its tier --
// active-passive doubles the MI bill regardless of utilization.
// =============================================================================

targetScope = 'resourceGroup'

@description('Resource name prefix.')
param prefix string = 'oramig'

@description('Primary region (where this RG and the primary MI live).')
param primaryRegion string = resourceGroup().location

@description('Secondary region (must be in MI failover-group pair list for primaryRegion).')
param secondaryRegion string = 'westus3'

@description('Resource ID of the existing primary MI (provisioned out-of-band).')
param primaryMiId string

@description('Resource ID of the existing secondary MI in secondaryRegion.')
param secondaryMiId string

@description('Failover policy. Manual = operator decides; Automatic = automated with the grace period below.')
@allowed(['Manual', 'Automatic'])
param failoverPolicy string = 'Manual'

@description('Read/write grace period in minutes. Only relevant when failoverPolicy = Automatic.')
@minValue(1)
@maxValue(1440)
param readWriteGracePeriodMin int = 60

@description('Failover-group name. Becomes the read/write listener prefix.')
param failoverGroupName string = '${prefix}-mi-fog'

// Microsoft.Sql/locations/instanceFailoverGroups is a child of the location
// pseudo-resource; we encode that via the slash-separated `name` and
// deploy at the primary MI's resource-group scope.
resource fog 'Microsoft.Sql/locations/instanceFailoverGroups@2023-08-01-preview' = {
  name: '${primaryRegion}/${failoverGroupName}'
  properties: {
    readWriteEndpoint: {
      failoverPolicy:                         failoverPolicy
      failoverWithDataLossGracePeriodMinutes: readWriteGracePeriodMin
    }
    readOnlyEndpoint: {
      // Disabled until the operator opts in by changing this to 'Enabled'
      // and routing read-only workloads to the .secondary listener.
      failoverPolicy: 'Disabled'
    }
    partnerRegions: [
      { location: secondaryRegion }
    ]
    managedInstancePairs: [
      {
        primaryManagedInstanceId: primaryMiId
        partnerManagedInstanceId: secondaryMiId
      }
    ]
  }
}

output failoverGroupName string = failoverGroupName
output readWriteListener  string = '${failoverGroupName}.${environment().suffixes.sqlServerHostname}'
output readOnlyListener   string = '${failoverGroupName}.secondary.${environment().suffixes.sqlServerHostname}'
