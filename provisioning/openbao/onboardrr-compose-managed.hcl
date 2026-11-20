# Add to the existing controller ACL after creating the managed auth mount.
path "hosts/data/+/compose-agent" {
  capabilities = ["read", "create"]
}

path "hosts/metadata/+/compose" {
  capabilities = ["list"]
}

path "hosts/data/+/compose/config" {
  capabilities = ["create"]
}

path "auth/onboardrr-compose/role/+" {
  capabilities = ["create", "update"]
  required_parameters = ["bind_secret_id", "token_no_default_policy", "token_policies", "secret_id_num_uses", "secret_id_ttl", "token_num_uses", "token_period"]
  allowed_parameters = {
    "bind_secret_id"          = ["true"]
    "token_no_default_policy" = ["true"]
    "token_policies"          = ["onboardrr-compose-host"]
    "secret_id_num_uses"       = ["0"]
    "secret_id_ttl"            = ["0"]
    "token_num_uses"           = ["0"]
    "token_period"             = ["3600"]
  }
}

path "auth/onboardrr-compose/role/+/role-id" {
  capabilities = ["read"]
}

path "auth/onboardrr-compose/role/+/secret-id" {
  capabilities = ["update"]
  denied_parameters = {
    "metadata"         = []
    "cidr_list"         = []
    "token_bound_cidrs" = []
    "num_uses"         = []
    "ttl"              = []
  }
}

path "auth/onboardrr-compose/role/+/secret-id-accessor/destroy" {
  capabilities = ["update"]
}
