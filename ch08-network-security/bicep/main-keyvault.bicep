// =============================================================================
// Azure Key Vault with RBAC + soft-delete + purge protection + Private Endpoint.
//
// Stores: GG credential aliases, target DB passwords, TLS certs, CMK material.
// Access pattern: workload hosts (GG, app VMs, build agents) use System-Assigned
// Managed Identity to read secrets via 'Key Vault Secrets User' role.
// No standing service principal credentials, no shared secrets in repos.
// =============================================================================

@description('Resource name prefix.')
param prefix string = 'oramig'

@description('Azure region.')
param location string = resourceGroup().location

@description('Object ID of the principal (user / group / SP) granted Secrets Officer at deploy time. Used for the initial secret population step only.')
param adminPrincipalObjectId string

@description('Resource ID of the spoke-db VNet PE subnet, for the Key Vault Private Endpoint.')
param peSubnetId string

@description('Resource ID of the privatelink.vaultcore.azure.net Private DNS zone.')
param kvPrivateDnsZoneId string

@description('Tags.')
param tags object = {
  project:   'oracle-to-azure-book'
  chapter:   'ch08-network-security'
  lifecycle: 'shared'
}

resource kv 'Microsoft.KeyVault/vaults@2023-07-01' = {
  name: '${prefix}kv${uniqueString(resourceGroup().id)}'
  location: location
  tags: tags
  properties: {
    sku: { family: 'A', name: 'standard' }
    tenantId: tenant().tenantId

    // RBAC, not access policies. Access policies are deprecated for new vaults.
    enableRbacAuthorization: true

    // Compliance baselines require BOTH soft-delete and purge protection.
    // Once purge protection is on, it cannot be turned off for the lifetime
    // of the vault. Make the choice deliberately.
    enableSoftDelete:        true
    softDeleteRetentionInDays: 90
    enablePurgeProtection:   true

    // Lock down public access. The PE below is the only ingress.
    publicNetworkAccess: 'Disabled'
    networkAcls: {
      defaultAction: 'Deny'
      bypass:        'AzureServices'
      ipRules:       []
      virtualNetworkRules: []
    }
  }
}

// Role assignment: 'Key Vault Secrets Officer' for the deploy-time admin
// (so 02_apply_keyvault.sh can populate initial secrets).
var roleSecretsOfficer = subscriptionResourceId(
  'Microsoft.Authorization/roleDefinitions',
  'b86a8fe4-44ce-4948-aee5-eccb2c155cd7')

resource roleAssign 'Microsoft.Authorization/roleAssignments@2022-04-01' = {
  scope: kv
  name: guid(kv.id, adminPrincipalObjectId, roleSecretsOfficer)
  properties: {
    principalId:      adminPrincipalObjectId
    roleDefinitionId: roleSecretsOfficer
    principalType:    'User'
  }
}

// Private Endpoint
resource kvPe 'Microsoft.Network/privateEndpoints@2023-11-01' = {
  name: '${prefix}-kv-pe'
  location: location
  tags: tags
  properties: {
    subnet: { id: peSubnetId }
    privateLinkServiceConnections: [
      {
        name: 'kv-plsc'
        properties: {
          privateLinkServiceId: kv.id
          groupIds: ['vault']
        }
      }
    ]
  }
}

resource kvPeDnsGroup 'Microsoft.Network/privateEndpoints/privateDnsZoneGroups@2023-11-01' = {
  parent: kvPe
  name: 'kv-pdz-group'
  properties: {
    privateDnsZoneConfigs: [
      {
        name: 'kv-dns'
        properties: { privateDnsZoneId: kvPrivateDnsZoneId }
      }
    ]
  }
}

output keyVaultName string = kv.name
output keyVaultUri  string = kv.properties.vaultUri
