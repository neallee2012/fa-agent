using './addon.bicep'

// Placeholders only; fill in per run. mockApiKey is a dummy shared with the hosted agent, not a secret.
param foundryAccountName = '<template-15 account name: aiServices plus its 4-character suffix>'
param apimName = 'apim-fagwprobe-<suffix>'
param publisherEmail = '<publisher email>'
param agentAppId = '00000000-0000-0000-0000-000000000000'  // stays a placeholder; step 3 updates the policy in place
param sshPublicKey = '<ssh-ed25519 public key>'
param deployNva = false
param mockApiKey = 'mock-dummy-key'
