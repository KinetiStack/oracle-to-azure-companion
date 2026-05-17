// budget.bicep -- Azure Consumption Budget for the migration's resource group.
//
// Deploys an actual-spend alert at 80% and 100% of the monthly budget,
// plus a forecasted-spend alert at 100%. Forecasted-spend alerts catch
// trajectory issues 1-2 weeks before they hit the monthly bill, which
// the actual-spend alert cannot.
//
// Scope: the resource group that holds the migrated target databases.
// Action Group: re-uses the cutover hypercare Action Group from Ch.12.
//
// Deploy (PowerShell or bash):
//   az deployment group create \
//       --resource-group <rg-name> \
//       --template-file budget.bicep \
//       --parameters \
//           budgetName=mig-rg-monthly \
//           monthlyAmountUsd=15000 \
//           actionGroupId=/subscriptions/.../microsoft.insights/actionGroups/cutover-hypercare \
//           costCenterTag=cc-44291
//
// NOTE: budgets are billed-scope objects -- they are NOT children of the
// resource group ARM-wise. They live at the same subscription scope but
// filter to the RG via the filter block.

targetScope = 'resourceGroup'

@description('Budget name (subscription-unique).')
param budgetName string

@description('Monthly amount in USD. Pick from quarterly FinOps forecast.')
param monthlyAmountUsd int

@description('Resource ID of the Action Group that receives alerts (re-use cutover hypercare AG).')
param actionGroupId string

@description('Cost-center tag value (filter scope).')
param costCenterTag string

@description('Start date (YYYY-MM-01). Must be the first of a month for monthly budgets.')
param startDate string = utcNow('yyyy-MM-01')

@description('End date (YYYY-MM-DD). Budget ends here. Default: 5 years.')
param endDate string = dateTimeAdd(utcNow('yyyy-MM-01'), 'P5Y', 'yyyy-MM-dd')

@description('Email recipients (in addition to the Action Group).')
param contactEmails array = []

resource budget 'Microsoft.Consumption/budgets@2023-11-01' = {
  name: budgetName
  properties: {
    timePeriod: {
      startDate: startDate
      endDate:   endDate
    }
    timeGrain: 'Monthly'
    amount:    monthlyAmountUsd
    category:  'Cost'
    // The targetScope='resourceGroup' already restricts the budget to
    // this RG -- no need to AND a ResourceGroupName dimension filter
    // here. We DO scope by cost-center tag so a shared RG (rare but
    // real for transitional Day-N estates) only counts the migration's
    // own resources.
    //
    // SIDE EFFECT, documented in the chapter prose (§13.5): resources
    // in this RG that lack the 'cost-center-id' tag are *excluded* from
    // the budget. This is intentional -- pair with the required-tags
    // Policy in `required_tags_policy.json` so untagged resources cannot
    // exist in steady state. Drop this filter entirely if you do not
    // share the RG across cost centers.
    filter: {
      tags: {
        name:     'cost-center-id'
        operator: 'In'
        values:   [ costCenterTag ]
      }
    }
    notifications: {
      actual_80: {
        enabled:        true
        operator:       'GreaterThanOrEqualTo'
        threshold:      80
        thresholdType:  'Actual'
        contactEmails:  contactEmails
        contactGroups:  [ actionGroupId ]
        locale:         'en-us'
      }
      actual_100: {
        enabled:        true
        operator:       'GreaterThanOrEqualTo'
        threshold:      100
        thresholdType:  'Actual'
        contactEmails:  contactEmails
        contactGroups:  [ actionGroupId ]
        locale:         'en-us'
      }
      forecast_100: {
        enabled:        true
        operator:       'GreaterThanOrEqualTo'
        threshold:      100
        thresholdType:  'Forecasted'
        contactEmails:  contactEmails
        contactGroups:  [ actionGroupId ]
        locale:         'en-us'
      }
    }
  }
}

output budgetId string = budget.id
output budgetName string = budget.name
