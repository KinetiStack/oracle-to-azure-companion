// =============================================================================
// Migrating Oracle Databases to Azure Cloud — Chapter 3.5 lab environment.
//
// Scope    : resource group
// Resources: Azure SQL Database (S1) + Azure DB for PostgreSQL Flex (B1ms)
//            with public endpoints and IP-firewall rules.
//
// Deploy (from a shell with az CLI):
//   az group create -n rg-oracle-lab -l eastus
//   az deployment group create -g rg-oracle-lab -f main.bicep \
//       --parameters adminPassword='<strong-password>' \
//                    allowedClientIp="$(curl -s https://api.ipify.org)"
//
// Cost envelope (East US, 2026-Q2 list pricing — verify before commit):
//   Azure SQL DB S1 ............. ~$30/month if left running 24x7
//   PG Flex B1ms ................ ~$15/month + ~$3 storage
//   Total: $1.50/day if running 8 hours; pennies if torn down nightly.
// =============================================================================

@description('Resource name prefix (must be 3-15 lowercase alphanumeric chars).')
param prefix string = 'oramig'

@description('Location for resources. Default: resource group location.')
param location string = resourceGroup().location

@description('Administrator login (used for both SQL and Postgres).')
param adminLogin string = 'labadmin'

@description('Administrator password. Use Key Vault reference in production.')
@secure()
param adminPassword string

@description('Public IP allowed to reach the targets. Empty string disables client IP firewall (Azure Services rule still applies).')
param allowedClientIp string = ''

@description('Tag values applied to all resources.')
param tags object = {
  project: 'oracle-to-azure-book'
  chapter: 'ch03-5-lab-setup'
  lifecycle: 'lab'
}

// ----- Azure SQL logical server + database (MI proxy for Ch.4 / Ch.5) ------
resource sqlServer 'Microsoft.Sql/servers@2023-08-01-preview' = {
  name: toLower('${prefix}sql${uniqueString(resourceGroup().id)}')
  location: location
  tags: tags
  properties: {
    administratorLogin: adminLogin
    administratorLoginPassword: adminPassword
    minimalTlsVersion: '1.2'
    publicNetworkAccess: 'Enabled'
    version: '12.0'
  }
}

resource sqlDb 'Microsoft.Sql/servers/databases@2023-08-01-preview' = {
  parent: sqlServer
  name: 'labdb'
  location: location
  tags: tags
  sku: {
    name: 'S1'
    tier: 'Standard'
  }
  properties: {}
}

resource sqlFwAzure 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = {
  parent: sqlServer
  name: 'AllowAzureServices'
  properties: {
    startIpAddress: '0.0.0.0'
    endIpAddress: '0.0.0.0'
  }
}

resource sqlFwClient 'Microsoft.Sql/servers/firewallRules@2023-08-01-preview' = if (!empty(allowedClientIp)) {
  parent: sqlServer
  name: 'AllowLabClient'
  properties: {
    startIpAddress: allowedClientIp
    endIpAddress: allowedClientIp
  }
}

// ----- Azure Database for PostgreSQL Flexible Server -----------------------
resource pgFlex 'Microsoft.DBforPostgreSQL/flexibleServers@2023-12-01-preview' = {
  name: toLower('${prefix}pg${uniqueString(resourceGroup().id)}')
  location: location
  tags: tags
  sku: {
    name: 'Standard_B1ms'
    tier: 'Burstable'
  }
  properties: {
    version: '16'
    administratorLogin: adminLogin
    administratorLoginPassword: adminPassword
    storage: {
      storageSizeGB: 32
    }
    backup: {
      backupRetentionDays: 7
      geoRedundantBackup: 'Disabled'
    }
    highAvailability: {
      mode: 'Disabled'
    }
    network: {
      publicNetworkAccess: 'Enabled'
    }
  }
}

resource pgFw 'Microsoft.DBforPostgreSQL/flexibleServers/firewallRules@2023-12-01-preview' = if (!empty(allowedClientIp)) {
  parent: pgFlex
  name: 'allow-lab-client'
  properties: {
    startIpAddress: allowedClientIp
    endIpAddress: allowedClientIp
  }
}

// ----- Outputs --------------------------------------------------------------
output sqlServerFqdn   string = sqlServer.properties.fullyQualifiedDomainName
output sqlDatabaseName string = sqlDb.name
output pgFlexFqdn      string = pgFlex.properties.fullyQualifiedDomainName
output adminLogin      string = adminLogin
