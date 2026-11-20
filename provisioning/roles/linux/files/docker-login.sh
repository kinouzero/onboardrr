#!/bin/sh
set -eu
umask 077

docker="$1"
docker_config="$2/.docker"
username="$3"
config="$docker_config/config.json"

case "$docker_config" in /*) ;; *) exit 9 ;; esac
[ ! -L "$docker_config" ] && { [ ! -e "$docker_config" ] || [ -d "$docker_config" ]; } || exit 9
[ ! -L "$config" ] && { [ ! -e "$config" ] || [ -f "$config" ]; } || exit 9
[ -d "$docker_config" ] || mkdir "$docker_config" || exit 9

unset DOCKER_CONTEXT DOCKER_HOST
"$docker" --host unix:///var/run/docker.sock --config "$docker_config" \
    login --username "$username" --password-stdin >/dev/null 2>&1 || exit 10
[ ! -f "$config" ] || chmod 0600 "$config" || exit 9
