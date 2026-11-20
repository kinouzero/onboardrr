# Substitute APPROLE_ACCESSOR with the accessor of auth/onboardrr-compose.
# AppRole sets role_name in its identity alias when the host logs in.
path "hosts/data/{{identity.entity.aliases.APPROLE_ACCESSOR.metadata.role_name}}/compose/*" {
  capabilities = ["read"]
}

path "hosts/data/{{identity.entity.aliases.APPROLE_ACCESSOR.metadata.role_name}}/periphery" {
  capabilities = ["read"]
}

path "hosts/data/{{identity.entity.aliases.APPROLE_ACCESSOR.metadata.role_name}}/HOSTNAME" {
  capabilities = ["read"]
}

path "hosts/data/common/USER" {
  capabilities = ["read"]
}

path "auth/token/renew-self" {
  capabilities = ["update"]
}

path "auth/token/lookup-self" {
  capabilities = ["read"]
}

path "auth/token/revoke-self" {
  capabilities = ["update"]
}
