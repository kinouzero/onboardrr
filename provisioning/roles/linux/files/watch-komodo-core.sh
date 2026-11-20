#!/bin/bash
set -euo pipefail

config=/run/komodo-openbao/rendered/core.config.toml
core_pid=
watch_pid=

stop() {
  if [[ -n "$watch_pid" ]]; then
    kill "$watch_pid" 2>/dev/null || true
  fi
  if [[ -n "$core_pid" ]]; then
    kill -TERM "$core_pid" 2>/dev/null || true
    wait "$core_pid" || true
  fi
}
trap 'stop; exit 0' TERM INT

until [[ -s "$config" ]]; do
  sleep 2
done
initial_version=$(sha256sum "$config")

watch_config() {
  local current_version
  while sleep 5; do
    if current_version=$(sha256sum "$config" 2>/dev/null) &&
      [[ "$current_version" != "$initial_version" ]]; then
      return
    fi
  done
}

# Let the Compose restart policy reload Core when the rendered TOML changes.
core &
core_pid=$!
watch_config &
watch_pid=$!
status=0
wait -n "$core_pid" "$watch_pid" || status=$?
stop
exit "$status"
