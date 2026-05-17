// =============================================================================
// Hub-Spoke topology + Private Endpoints + Private DNS for the migration estate.
//
// Scope: resource group
// Deploys:
//   - Hub VNet (10.42.0.0/22) with Gateway/Firewall/Bastion + mgmt subnets
//   - Spoke VNet (10.43.0.0/22) with pe-subnet + gg-subnet
//   - Bi-directional VNet peering
//   - Network Security Group on the PE subnet (deny-all + explicit allows)
//   - Private DNS zones for SQL, PostgreSQL, Key Vault, Blob
//   - Private Endpoints for the Ch.3.5 SQL Server and PG Flex
//
// NOT deployed here:
//   - ExpressRoute circuit -- created at the ExpressRoute provider; this
//     template assumes the GatewaySubnet is in place to attach one.
//   - Azure Firewall / Bastion instances -- subnets are reserved; instances
//     are workload-specific and outside the scope of this template.
//
// Cost envelope (East US 2026-Q2; verify before commit):
//   VNet + peering: free (peering data per GB)
//   Private DNS zones: ~$0.50/month per zone
//   Private Endpoints:  ~$0.01/hour each (~$7.50/month per PE)
//   Total: ~$30-40/month for the network surface, before any compute.
// =============================================================================

@description('Resource name prefix.')
param prefix string = 'oramig'

@description('Azure region.')
param location string = resourceGroup().location

@description('On-prem source CIDR allowed inbound to the PE subnet.')
param onpremCidr string = '10.10.0.0/16'

@description('Name of the SQL Server logical resource from the Ch.3.5 deploy.')
param sqlServerName string

@description('Name of the PG Flex server from the Ch.3.5 deploy.')
param pgFlexServerName string

@description('Tag values applied to all resources.')
param tags object = {
  project:   'oracle-to-azure-book'
  chapter:   'ch08-network-security'
  lifecycle: 'shared'
}

// -----------------------------------------------------------------------------
// Hub VNet
// -----------------------------------------------------------------------------
resource hub 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: '${prefix}-hub-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.42.0.0/22'] }
    subnets: [
      { name: 'GatewaySubnet',       properties: { addressPrefix: '10.42.0.0/26' } }
      { name: 'AzureFirewallSubnet', properties: { addressPrefix: '10.42.0.64/26' } }
      { name: 'AzureBastionSubnet',  properties: { addressPrefix: '10.42.0.128/26' } }
      { name: 'mgmt-subnet',         properties: { addressPrefix: '10.42.1.0/24' } }
    ]
  }
}

// -----------------------------------------------------------------------------
// NSG for the PE subnet (deny-all-inbound + explicit allows)
// -----------------------------------------------------------------------------
resource nsgPe 'Microsoft.Network/networkSecurityGroups@2023-11-01' = {
  name: '${prefix}-pe-nsg'
  location: location
  tags: tags
  properties: {
    securityRules: [
      {
        name: 'allow-onprem-sql-1433'
        properties: {
          priority: 100, direction: 'Inbound', access: 'Allow', protocol: 'Tcp'
          sourceAddressPrefix: onpremCidr
          sourcePortRange: '*'
          destinationAddressPrefix: '10.43.0.0/24'
          destinationPortRange: '1433'
        }
      }
      {
        name: 'allow-onprem-pg-5432'
        properties: {
          priority: 110, direction: 'Inbound', access: 'Allow', protocol: 'Tcp'
          sourceAddressPrefix: onpremCidr
          sourcePortRange: '*'
          destinationAddressPrefix: '10.43.0.0/24'
          destinationPortRange: '5432'
        }
      }
      {
        name: 'allow-gg-subnet'
        properties: {
          priority: 120, direction: 'Inbound', access: 'Allow', protocol: 'Tcp'
          sourceAddressPrefix: '10.43.1.0/24'
          sourcePortRange: '*'
          destinationAddressPrefix: '10.43.0.0/24'
          destinationPortRange: '*'
        }
      }
      {
        name: 'deny-all-inbound'
        properties: {
          priority: 4096, direction: 'Inbound', access: 'Deny', protocol: '*'
          sourceAddressPrefix: '*', sourcePortRange: '*'
          destinationAddressPrefix: '*', destinationPortRange: '*'
        }
      }
    ]
  }
}

// -----------------------------------------------------------------------------
// Spoke VNet for DB workloads
// -----------------------------------------------------------------------------
resource spokeDb 'Microsoft.Network/virtualNetworks@2023-11-01' = {
  name: '${prefix}-spoke-db-vnet'
  location: location
  tags: tags
  properties: {
    addressSpace: { addressPrefixes: ['10.43.0.0/22'] }
    subnets: [
      {
        name: 'pe-subnet'
        properties: {
          addressPrefix: '10.43.0.0/24'
          privateEndpointNetworkPolicies: 'Disabled'   // required for PE traffic
          networkSecurityGroup: { id: nsgPe.id }
        }
      }
      {
        name: 'gg-subnet'
        properties: { addressPrefix: '10.43.1.0/24' }
      }
    ]
  }
}

// -----------------------------------------------------------------------------
// VNet peering (bi-directional)
// -----------------------------------------------------------------------------
resource peerHub2Spoke 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2023-11-01' = {
  parent: hub
  name: 'hub-to-spoke-db'
  properties: {
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: true
    remoteVirtualNetwork: { id: spokeDb.id }
  }
}
resource peerSpoke2Hub 'Microsoft.Network/virtualNetworks/virtualNetworkPeerings@2023-11-01' = {
  parent: spokeDb
  name: 'spoke-db-to-hub'
  properties: {
    allowVirtualNetworkAccess: true
    allowForwardedTraffic: true
    remoteVirtualNetwork: { id: hub.id }
  }
}

// -----------------------------------------------------------------------------
// Private DNS zones (must exist before PEs are linked)
// -----------------------------------------------------------------------------
var privateZones = [
  'privatelink.database.windows.net'
  'privatelink.postgres.database.azure.com'
  'privatelink.vaultcore.azure.net'
  'privatelink.blob.${environment().suffixes.storage}'
]

resource pdz 'Microsoft.Network/privateDnsZones@2020-06-01' = [for zone in privateZones: {
  name: zone
  location: 'global'
  tags: tags
}]

resource pdzLink 'Microsoft.Network/privateDnsZones/virtualNetworkLinks@2020-06-01' = [for (zone, i) in privateZones: {
  parent: pdz[i]
  name: 'spoke-db-link'
  location: 'global'
  properties: {
    registrationEnabled: false
    virtualNetwork: { id: spokeDb.id }
  }
}]

// -----------------------------------------------------------------------------
// Private Endpoints for the Ch.3.5 targets
// -----------------------------------------------------------------------------
resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' existing = {
  name: sqlServerName
}
resource pgFlex 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' existing = {
  name: pgFlexServerName
}

resource peSql 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: '${prefix}-sql-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: '${spokeDb.id}/subnets/pe-subnet' }
    privateLinkServiceConnections: [
      {
        name: 'sql-plsc'
        properties: {
          privateLinkServiceId: sqlServer.id
          groupIds: ['sqlServer']
        }
      }
    ]
  }
}

resource peSqlDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: peSql
  name: 'sql-pdz-group'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'sql-dns'
        properties: { privateDnsZoneId: pdz[0].id }
      }
    ]
  }
}

resource pePg 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: '${prefix}-pg-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: '${spokeDb.id}/subnets/pe-subnet' }
    privateLinkServiceConnections: [
      {
        name: 'pg-plsc'
        properties: {
          privateLinkServiceId: pgFlex.id
          groupIds: ['postgresqlServer']
        }
      }
    ]
  }
}

resource pePgDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: pePg
  name: 'pg-pdz-group'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'pg-dns'
        properties: { privateDnsZoneId: pdz[1].id }
      }
    ]
  }
}

output hubVnetId         string = hub.id
output spokeDbVnetId     string = spokeDb.id
output peSqlIp           string = peSql.properties.customDnsConfigs[0].ipAddresses[0]
output pePgIp            string = pePg.properties.customDnsConfigs[0].ipAddresses[0]
