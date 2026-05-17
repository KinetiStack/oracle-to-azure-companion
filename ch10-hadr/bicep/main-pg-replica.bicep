// =============================================================================
// Azure Database for PostgreSQL Flexible Server cross-region read replica.
//
// PG Flex's HA/DR primitives differ from MI:
//   - In-region HA  -> 'highAvailability.mode = ZoneRedundant' (only on
//                       Memory-Optimized; Burstable tier does NOT support it)
//   - Cross-region  -> 'createMode = Replica' against the primary's serverId
//                       (async streaming). Promotion is MANUAL via
//                       'az postgres flexible-server replica promote' or via
//                       this Bicep with promoteMode flipped.
//
// There is no built-in failover group equivalent for PG Flex; the operator
// orchestrates promotion + DNS swing + application reconnect.
// =============================================================================

@description('Resource name prefix.')
param prefix string = 'oramig'

@description('Region for the cross-region read replica.')
param replicaRegion string = 'westus3'

@description('Primary PG Flex server resource ID.')
param primaryServerId string

@description('Replica SKU name. MUST match the source server tier -- PG Flex rejects cross-tier replication. Lab default is Burstable to match Ch.3.5; production swaps to MemoryOptimized once the primary is upgraded.')
param replicaSkuName string = 'Standard_B1ms'

@description('Replica SKU tier. Must equal the source tier.')
@allowed(['Burstable', 'GeneralPurpose', 'MemoryOptimized'])
param replicaSkuTier string = 'Burstable'

@description('Storage size GiB on the replica. Should be >= primary storage.')
param replicaStorageGib int = 32

// Replica creation inherits administratorLogin from the source server.
// Setting it explicitly on createMode='Replica' is either ignored or
// (on some API versions) rejected with "administratorLogin cannot be
// changed on Replica" -- so we omit it entirely.
resource replica 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: '${prefix}-pg-replica-${uniqueString(replicaRegion)}'
  location: replicaRegion
  sku: { name: replicaSkuName, tier: replicaSkuTier }
  properties: {
    createMode:             'Replica'
    sourceServerResourceId: primaryServerId
    storage:                { storageSizeGB: replicaStorageGib }
    backup:                 { backupRetentionDays: 7, geoRedundantBackup: 'Disabled' }
    highAvailability:       { mode: 'Disabled' }   // replica HA is the primary's job
  }
}

output replicaName string = replica.name
output replicaFqdn string = replica.properties.fullyQualifiedDomainName
