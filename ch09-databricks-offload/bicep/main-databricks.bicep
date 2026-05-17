// =============================================================================
// Azure Databricks workspace + ADLS Gen2 storage account for the Medallion lake.
//
// *** P19: isHnsEnabled MUST be true at creation time. The hierarchical
// *** namespace cannot be enabled after the storage account exists. Verify
// *** isHnsEnabled below before deploying -- the decision is irreversible.
//
// Scope: resource group
// Deploy with:
//   az group create -n rg-oracle-modern -l eastus
//   az deployment group create -g rg-oracle-modern -f main-databricks.bicep
// =============================================================================

@description('Resource name prefix.')
param prefix string = 'oramig'

@description('Azure region.')
param location string = resourceGroup().location

@description('Databricks pricing tier. premium enables Unity Catalog + RBAC.')
@allowed(['standard', 'premium'])
param dbxSku string = 'premium'

@description('Tag values applied to all resources.')
param tags object = {
  project:   'oracle-to-azure-book'
  chapter:   'ch09-databricks-offload'
  lifecycle: 'modernization'
}

// -----------------------------------------------------------------------------
// ADLS Gen2 storage account. isHnsEnabled = true is the irreversible decision.
// -----------------------------------------------------------------------------
resource lake 'Microsoft.Storage/storageAccounts@2023-05-01' = {
  name: toLower('${prefix}lake${uniqueString(resourceGroup().id)}')
  location: location
  tags: tags
  kind: 'StorageV2'
  sku: {
    name: 'Standard_LRS'   // dev/lab; production uses Standard_ZRS or GZRS
  }
  properties: {
    isHnsEnabled:          true     // *** ADLS Gen2: enable hierarchical namespace
    minimumTlsVersion:     'TLS1_2'
    allowBlobPublicAccess: false
    supportsHttpsTrafficOnly: true
    accessTier:            'Hot'
    networkAcls: {
      defaultAction: 'Allow'   // tighten via PE in production
      bypass:        'AzureServices'
    }
  }
}

// Three filesystems (Bronze / Silver / Gold) under the lake account.
var filesystems = ['bronze', 'silver', 'gold']

resource lakeFs 'Microsoft.Storage/storageAccounts/blobServices/containers@2023-05-01' = [for fs in filesystems: {
  name: '${lake.name}/default/${fs}'
  properties: {
    publicAccess: 'None'
  }
}]

// -----------------------------------------------------------------------------
// Databricks workspace. Premium SKU gates Unity Catalog access.
// The "managed resource group" referenced via managedResourceGroupId is
// auto-created at deploy time by the Databricks resource provider -- no
// pre-existing RG declaration is required (or desirable).
// -----------------------------------------------------------------------------
resource dbx 'Microsoft.Databricks/workspaces@2024-05-01' = {
  name: '${prefix}-dbx-${uniqueString(resourceGroup().id)}'
  location: location
  tags: tags
  sku: { name: dbxSku }
  properties: {
    // Managed RG name passed as an ID; Databricks RP creates it on deploy
    // and owns its contents (VNet, NSG, public IPs). The ID is constructed
    // by subscription-scope helper so the deployer doesn't pre-declare it.
    managedResourceGroupId: subscriptionResourceId('Microsoft.Resources/resourceGroups',
                                                   '${resourceGroup().name}-dbx-managed')
    parameters: {
      enableNoPublicIp: { value: true }   // private cluster nodes; required
                                          // for production / Unity Catalog
    }
  }
}

output lakeAccountName     string = lake.name
output lakeAccountId       string = lake.id
output databricksWorkspace string = dbx.name
output databricksUrl       string = 'https://${dbx.properties.workspaceUrl}'
