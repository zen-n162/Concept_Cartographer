// Concept Cartographer — ACA internal-only ingress (メモ §11 推奨方式)
//
//   Foundry (Standard Agent Setup / Private Networking)
//     -> ACA internal ingress -> excalidraw-mcp (/mcp, /healthz) + cc-tools (/mcp)
//
// セキュリティ制約 (メモ §4): Public IP なし / インターネット非公開。
// internal: true の ACA 環境は VNet 内からのみ到達可能。
//
// 前提 (管理者調整が必要):
//   - ACA 用サブネット (/23 以上推奨, Microsoft.App/environments に委任)
//   - Foundry からこの VNet への経路 (同一 VNet or peering + Private DNS)
//
// deploy:
//   az deployment group create -g prj-qst-ai -f infra/main.bicep \
//     -p infraSubnetId=/subscriptions/.../subnets/snet-cc-aca \
//     -p acrName=<acr>

@description('ACA を配置するサブネットの resource ID (Microsoft.App 委任済み)')
param infraSubnetId string

@description('コンテナイメージを置く ACR 名')
param acrName string

param location string = resourceGroup().location
param envName string = 'cae-concept-cartographer'
param excalidrawImage string = 'excalidraw-mcp:latest'
param ccToolsImage string = 'cc-tools:latest'

resource logws 'Microsoft.OperationalInsights/workspaces@2023-09-01' = {
  name: 'log-concept-cartographer'
  location: location
  properties: {
    sku: { name: 'PerGB2018' }
    retentionInDays: 30
  }
}

resource acr 'Microsoft.ContainerRegistry/registries@2023-11-01-preview' existing = {
  name: acrName
}

resource env 'Microsoft.App/managedEnvironments@2024-03-01' = {
  name: envName
  location: location
  properties: {
    vnetConfiguration: {
      infrastructureSubnetId: infraSubnetId
      internal: true // ★ internal-only: public endpoint を作らない
    }
    appLogsConfiguration: {
      destination: 'log-analytics'
      logAnalyticsConfiguration: {
        customerId: logws.properties.customerId
        sharedKey: logws.listKeys().primarySharedKey
      }
    }
  }
}

var registryConfig = [
  {
    server: acr.properties.loginServer
    identity: 'system'
  }
]

resource excalidrawApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'excalidraw-mcp'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: false // 環境自体 internal + app も environment 内公開のみ
        targetPort: 8000
        transport: 'http'
        allowInsecure: false
      }
      registries: registryConfig
    }
    template: {
      containers: [
        {
          name: 'excalidraw-mcp'
          image: '${acr.properties.loginServer}/${excalidrawImage}'
          resources: { cpu: json('0.5'), memory: '1Gi' }
          probes: [
            {
              type: 'Liveness'
              httpGet: { path: '/healthz', port: 8000 }
              initialDelaySeconds: 10
              periodSeconds: 30
            }
          ]
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 } // canvas はステートフル (in-memory scene)
    }
  }
}

resource ccToolsApp 'Microsoft.App/containerApps@2024-03-01' = {
  name: 'cc-tools'
  location: location
  identity: { type: 'SystemAssigned' }
  properties: {
    managedEnvironmentId: env.id
    configuration: {
      ingress: {
        external: false
        targetPort: 8080
        transport: 'http'
        allowInsecure: false
      }
      registries: registryConfig
    }
    template: {
      containers: [
        {
          name: 'cc-tools'
          image: '${acr.properties.loginServer}/${ccToolsImage}'
          env: [
            {
              name: 'EXCALIDRAW_MCP_URL'
              value: 'https://excalidraw-mcp.internal.${env.properties.defaultDomain}/mcp'
            }
          ]
          resources: { cpu: json('0.5'), memory: '1Gi' }
        }
      ]
      scale: { minReplicas: 1, maxReplicas: 1 }
    }
  }
}

output excalidrawMcpUrl string = 'https://excalidraw-mcp.internal.${env.properties.defaultDomain}/mcp'
output ccToolsMcpUrl string = 'https://cc-tools.internal.${env.properties.defaultDomain}/mcp'
output environmentStaticIp string = env.properties.staticIp
output privateDnsNote string = 'internal.${env.properties.defaultDomain} を Private DNS Zone として VNet にリンクし、A レコードを staticIp に向けること'
