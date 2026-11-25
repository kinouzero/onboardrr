# Add to the controller policy after enabling the dedicated AppRole mount.
path "auth/onboardrr-komodo/role/+" {
  capabilities = ["create", "update"]
  required_parameters = ["bind_secret_id", "token_no_default_policy", "token_policies", "token_period"]
  allowed_parameters = {
    "bind_secret_id"          = [true]
    "token_no_default_policy" = [true]
    "token_policies"          = ["onboardrr-komodo-core"]
    "token_period"            = ["3600"]
  }
}

path "auth/onboardrr-komodo/role/+/role-id" {
  capabilities = ["read"]
}

path "auth/onboardrr-komodo/role/+/secret-id" {
  capabilities = ["update"]
  denied_parameters = {
    "metadata"         = []
    "cidr_list"         = []
    "token_bound_cidrs" = []
    "num_uses"         = []
    "ttl"              = []
  }
}

path "auth/onboardrr-komodo/role/+/secret-id-accessor/destroy" {
  capabilities = ["update"]
}
