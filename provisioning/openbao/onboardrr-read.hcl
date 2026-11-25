path "hosts/data/+/smb" {
  capabilities = ["read"]
}

path "services/data/storage/IP" {
  capabilities = ["read", "create", "patch"]
}

path "hosts/data/+/periphery" {
  capabilities = ["read", "create"]
}

path "hosts/data/common/wifi/2G" {
  capabilities = ["read"]
}

path "hosts/data/common/wifi/5G" {
  capabilities = ["read"]
}

path "services/data/beszel/onboardrr" {
  capabilities = ["read"]
}

path "services/data/beszel/PASSWORD" {
  capabilities = ["read"]
}

path "services/data/beszel/AGENT_KEY" {
  capabilities = ["read"]
}

path "services/data/docker/API_PORT" {
  capabilities = ["read"]
}

path "services/data/dockerhub/USERNAME" {
  capabilities = ["read"]
}

path "services/data/dockerhub/TOKEN" {
  capabilities = ["read"]
}

path "services/data/node-exporter/PORT" {
  capabilities = ["read"]
}

path "services/data/beszel/AGENT_PORT" {
  capabilities = ["read"]
}

path "services/data/komodo/onboardrr" {
  capabilities = ["read"]
}

path "services/data/pangolin/onboardrr" {
  capabilities = ["read"]
}

path "services/data/wud/onboardrr" {
  capabilities = ["read"]
}

path "services/data/termix/onboardrr" {
  capabilities = ["read"]
}

path "services/data/portabase/onboardrr" {
  capabilities = ["read"]
}

path "services/data/wud/PASSWORD" {
  capabilities = ["read"]
}

path "hosts/data/+/HOSTNAME" {
  capabilities = ["read", "create"]
}

path "hosts/data/+/IP" {
  capabilities = ["read", "create", "patch"]
}

path "hosts/data/+/HOST_IP" {
  capabilities = ["read"]
}
