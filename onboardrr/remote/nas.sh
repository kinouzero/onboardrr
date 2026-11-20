#!/bin/sh
set -eu

[ -x /sbin/getcfg ] && [ -r /etc/config/qpkg.conf ] || exit 2
firmware=$(/sbin/getcfg System Version -f /etc/config/uLinux.conf)
case "$firmware" in
    [0-9]*.[0-9]*) ;;
    *) exit 2 ;;
esac

opkg_ready() {
    [ -x /opt/bin/opkg ] && /opt/bin/opkg --version >/dev/null 2>&1
}

python_ready() {
    PATH=/opt/bin:$PATH python3 -c 'import sys; sys.exit(sys.version_info < (3, 9))' >/dev/null 2>&1
}

entware_state() {
    for package in Entware Entware-std Entware-alt Entware-ng Entware-3x Optware Qnapware; do
        if [ -n "$(/sbin/getcfg "$package" Install_Path -d '' -f /etc/config/qpkg.conf)" ]; then
            case "$package" in
                Entware*) if opkg_ready; then echo ready; else echo conflict; fi ;;
                *) echo conflict ;;
            esac
            return
        fi
    done
    if [ -L /opt ] || { [ -e /opt ] && { [ ! -d /opt ] || [ -n "$(ls -A /opt)" ]; }; }; then
        echo conflict
        return
    fi
    case "$(uname -m)" in
        aarch64|armv5tel|armv7l|i[3-6]86|x86_64) ;;
        *) echo unsupported; return ;;
    esac
    if [ -x /sbin/qpkg_cli ] && command -v curl >/dev/null && command -v sha256sum >/dev/null; then
        echo missing
    else
        echo tools-missing
    fi
}

case "$1" in
    compose-home)
        printf 'home=%s\nuid=%s\ngid=%s\n' "$HOME" "$(id -u)" "$(id -g)"
        ;;
    compose-local)
        [ -d /share/compose ] || exit 3
        docker="$HOME/.docker"
        target="$docker/compose"
        [ ! -L "$docker" ] || exit 3
        mkdir -p "$docker"
        if [ -L "$target" ]; then
            [ "$(readlink "$target")" = /share/compose ] || exit 3
        else
            if [ -e "$target" ]; then
                rmdir "$target" || exit 3
            fi
            ln -s /share/compose "$target"
        fi
        printf 'ready=yes\n'
        ;;
    compose-mount)
        PATH=/opt/bin:/usr/bin:/usr/sbin:/bin:/sbin
        export PATH
        exec python3 -c "$2"
        ;;
    inspect)
        printf 'firmware=%s\narchitecture=%s\nuid=%s\n' "$firmware" "$(uname -m)" "$(id -u)"
        scope=${2:-all}
        if [ "$scope" = all ] || [ "$scope" = system ]; then
            state=$(entware_state)
            printf 'entware_state=%s\n' "$state"
            printf 'entware=%s\n' "$(if [ "$state" = ready ]; then echo yes; else echo no; fi)"
            printf 'python=%s\n' "$(if python_ready; then echo yes; else echo no; fi)"
        fi
        [ "$scope" != system ] || exit 0
        station=$(/sbin/getcfg container-station Enable -d FALSE -f /etc/config/qpkg.conf)
        path=$(/sbin/getcfg container-station Install_Path -d '' -f /etc/config/qpkg.conf)
        printf 'container_station=%s\n' "$station"
        unset DOCKER_HOST DOCKER_CONTEXT
        if [ "$station" = TRUE ] && [ -n "$path" ] && [ -x "$path/bin/docker" ]; then
            docker="$path/bin/docker"
            engine=$("$docker" --host unix:///var/run/docker.sock version --format '{{.Server.Version}}' 2>/dev/null) || engine=
            compose=$("$docker" compose version --short 2>/dev/null) || compose=
            if [ -z "$compose" ] && [ -x "$path/bin/docker-compose" ]; then
                compose=$("$path/bin/docker-compose" version --short 2>/dev/null) || compose=
            fi
            printf 'docker=%s\ncompose=%s\n' "$engine" "$compose"
        fi
        ;;
    install-entware)
        [ "$(id -u)" = 0 ] || exit 4
        state=$(entware_state)
        if [ "$state" = ready ]; then
            printf 'changed=no\n'
            exit 0
        fi
        [ "$state" = missing ] || exit 5
        umask 077
        work=$(mktemp -d /tmp/onboardrr-entware.XXXXXX) || exit 6
        trap 'rm -rf "$work"' EXIT
        trap 'exit 6' HUP INT TERM
        package="$work/Entware_1.03a_alt.qpkg"
        url=https://bin.entware.net/other/Entware_1.03a_alt.qpkg
        sha256=c6361eeb0d23d77bf35fa190901c3477b1451a1ae290ef06dec518b894c38ba8
        curl --fail --silent --show-error --proto '=https' --connect-timeout 10 --max-time 45 \
            --max-filesize 58238 --output "$package" "$url" || exit 6
        printf '%s  %s\n' "$sha256" "$package" | sha256sum -c - >/dev/null || exit 6
        [ "$(entware_state)" = missing ] || exit 5
        /sbin/qpkg_cli --manually "$package" >/dev/null || exit 7
        remaining=90
        until [ "$(/sbin/getcfg Entware Enable -d FALSE -f /etc/config/qpkg.conf)" = TRUE ] && opkg_ready; do
            [ "$remaining" -gt 0 ] || exit 7
            sleep 2
            remaining=$((remaining - 1))
        done
        printf 'changed=yes\n'
        ;;
    docker-login)
        path=$(/sbin/getcfg container-station Install_Path -d '' -f /etc/config/qpkg.conf)
        [ -n "$path" ] && [ -x "$path/bin/docker" ] || exit 9
        exec /bin/sh -c "$3" onboardrr-docker-login "$path/bin/docker" "$HOME" "$2"
        ;;
    install-python)
        if python_ready; then
            printf 'changed=no\n'
            exit 0
        fi
        [ "$(id -u)" = 0 ] && opkg_ready || exit 11
        /opt/bin/opkg update >/dev/null && /opt/bin/opkg install python3 >/dev/null && python_ready || exit 11
        printf 'changed=yes\n'
        ;;
    stage-monitoring)
        umask 077
        project=${3:-onboardrr-monitoring}
        case "$project" in onboardrr-monitoring|onboardrr-monitoring-beszel-agent|onboardrr-monitoring-node-exporter|onboardrr-monitoring-docker-telemetry) ;; *) exit 8 ;; esac
        for path in "$HOME/.config" "$HOME/.config/onboardrr" "$HOME/.config/onboardrr/monitoring"; do
            [ ! -L "$path" ] && { [ ! -e "$path" ] || [ -d "$path" ]; } || exit 8
            [ -d "$path" ] || mkdir "$path"
        done
        directory="$HOME/.config/onboardrr/monitoring"
        compose="$directory/compose.json"
        if [ "$project" != onboardrr-monitoring ]; then
            compose="$directory/$project.json"
        fi
        [ ! -L "$compose" ] && { [ ! -e "$compose" ] || [ -f "$compose" ]; } || exit 8
        [ -f "$compose" ] || [ "$2" = new ] || exit 8
        staged=$(mktemp "$directory/.onboardrr-compose.XXXXXX")
        trap 'rm -f "$staged"' EXIT
        cat > "$staged"
        if [ -f "$compose" ]; then
            cmp -s "$compose" "$staged" || exit 8
        else
            ln "$staged" "$compose" || exit 8
        fi
        printf 'compose=%s\n' "$compose"
        ;;
    start-monitoring)
        compose="$2"
        project=${3:-onboardrr-monitoring}
        case "$project:$compose" in
            onboardrr-monitoring:/*/.config/onboardrr/monitoring/compose.json) ;;
            onboardrr-monitoring-beszel-agent:/*/.config/onboardrr/monitoring/onboardrr-monitoring-beszel-agent.json|\
            onboardrr-monitoring-node-exporter:/*/.config/onboardrr/monitoring/onboardrr-monitoring-node-exporter.json|\
            onboardrr-monitoring-docker-telemetry:/*/.config/onboardrr/monitoring/onboardrr-monitoring-docker-telemetry.json) ;;
            *) exit 8 ;;
        esac
        [ -f "$compose" ] && [ ! -L "$compose" ] || exit 8
        path=$(/sbin/getcfg container-station Install_Path -d '' -f /etc/config/qpkg.conf)
        docker="$path/bin/docker"
        [ -n "$path" ] && [ -x "$docker" ] || exit 3
        unset DOCKER_CONTEXT DOCKER_HOST COMPOSE_FILE COMPOSE_PROJECT_NAME
        set --
        if [ "$project" != onboardrr-monitoring ]; then
            service=${project#onboardrr-monitoring-}
            legacy=$( "$docker" --host unix:///var/run/docker.sock ps --all --quiet \
                --filter label=com.docker.compose.project=onboardrr-monitoring \
                --filter "label=com.docker.compose.service=$service") || exit 3
            if [ -n "$legacy" ]; then
                compose="${compose%/*}/compose.json"
                [ -f "$compose" ] && [ ! -L "$compose" ] || exit 8
                project=onboardrr-monitoring
                set -- "$service"
            fi
        fi
        containers=$("$docker" --host unix:///var/run/docker.sock ps --all --quiet \
            --filter "label=com.docker.compose.project=$project") || exit 3
        for container in $containers; do
            owner=$("$docker" --host unix:///var/run/docker.sock inspect --format \
                '{{ index .Config.Labels "com.docker.compose.project.config_files" }}' "$container") || exit 3
            [ "$owner" = "$compose" ] || exit 8
        done
        if "$docker" compose version --short >/dev/null 2>&1; then
            "$docker" --host unix:///var/run/docker.sock compose -p "$project" -f "$compose" \
                up --detach --no-recreate "$@" >/dev/null
        else
            DOCKER_HOST=unix:///var/run/docker.sock "$path/bin/docker-compose" -p "$project" -f "$compose" \
                up --detach --no-recreate "$@" >/dev/null
        fi
        ;;
    apply)
        umask 077
        profile="$HOME/.profile"
        host="$2"
        case "$host" in ''|*[!a-z0-9-]*) exit 3 ;; esac
        for path in "$HOME/.docker" "$HOME/.docker/compose"; do
            if [ "$path" = "$HOME/.docker/compose" ] && [ -L "$path" ] &&
                [ "$(readlink "$path")" = /share/compose ] && [ -d "$path" ]; then
                continue
            fi
            [ ! -L "$path" ] && { [ ! -e "$path" ] || [ -d "$path" ]; } || exit 3
        done
        [ ! -L "$profile" ] && { [ ! -e "$profile" ] || [ -f "$profile" ]; } || exit 3
        changed=no
        for path in "$HOME/.docker" "$HOME/.docker/compose"; do
            if [ ! -d "$path" ]; then
                mkdir "$path"
                changed=yes
            fi
        done
        if ! grep -Eq '^[[:space:]]*alias[[:space:]]+opkg=' "$profile" 2>/dev/null; then
            printf '\n%s\n' 'alias opkg=/opt/bin/opkg' >> "$profile"
            changed=yes
        fi
        bin="$HOME/.config/onboardrr/bin"
        for path in "$HOME/.config" "$HOME/.config/onboardrr" "$bin"; do
            [ ! -L "$path" ] && { [ ! -e "$path" ] || [ -d "$path" ]; } || exit 3
            [ -d "$path" ] || mkdir "$path"
        done
        launcher="$bin/onboardrr-dcu"
        docker_cli="$bin/docker"
        [ ! -L "$launcher" ] && { [ ! -e "$launcher" ] || [ -f "$launcher" ]; } || exit 3
        [ ! -L "$docker_cli" ] && { [ ! -e "$docker_cli" ] || [ -f "$docker_cli" ]; } || exit 3
        staged=$(mktemp "$bin/.onboardrr-cli.XXXXXX")
        trap 'rm -f "$staged"' EXIT
        cat > "$staged"
        [ -s "$staged" ] || exit 3
        if ! cmp -s "$staged" "$launcher"; then
            mv -f "$staged" "$launcher"
            staged=$(mktemp "$bin/.onboardrr-cli.XXXXXX")
            changed=yes
        fi
        cat > "$staged" <<'DOCKER'
#!/bin/sh
station=$(/sbin/getcfg container-station Install_Path -d '' -f /etc/config/qpkg.conf) || exit 1
case "$station" in /*) ;; *) echo 'Container Station is unavailable.' >&2; exit 1 ;; esac
unset DOCKER_CONTEXT
export DOCKER_HOST=unix:///var/run/docker.sock
if [ "${1:-}" = compose ] && [ -x "$station/bin/docker-compose" ] &&
    ! "$station/bin/docker" compose version --short >/dev/null 2>&1; then
    shift
    exec "$station/bin/docker-compose" "$@"
fi
exec "$station/bin/docker" "$@"
DOCKER
        if ! cmp -s "$staged" "$docker_cli" || [ ! -x "$docker_cli" ]; then
            chmod 0700 "$staged"
            mv -f "$staged" "$docker_cli"
            changed=yes
        fi
        path_line="export PATH=\"\$HOME/.config/onboardrr/bin:\$PATH\""
        if ! grep -Fxq "$path_line" "$profile"; then
            printf '\n%s\n' "$path_line" >> "$profile"
            changed=yes
        fi
        if ! grep -Eq '^[[:space:]]*alias[[:space:]]+dcu=' "$profile"; then
            printf '\n%s\n' "alias dcu='PATH=/opt/bin:\$PATH python3 \"\$HOME/.config/onboardrr/bin/onboardrr-dcu\" --nas'" >> "$profile"
            changed=yes
        fi
        if ! grep -Eq '^[[:space:]]*alias[[:space:]]+cdcompose=' "$profile"; then
            printf '\n%s\n' "alias cdcompose='cd \"\$(PATH=/opt/bin:\$PATH python3 \"\$HOME/.config/onboardrr/bin/onboardrr-dcu\" --workspace $host)\"'" >> "$profile"
            changed=yes
        fi
        printf 'changed=%s\n' "$changed"
        ;;
    *) exit 2 ;;
esac
