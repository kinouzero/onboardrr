#!/bin/sh
# Disable only the recorded device's dedicated QTS NFSv4 export.
set -eu

name=$1
client=$2
mode=$3
share="onboardrr-$name"
export_path="/share/NFSv=4/$share"
settings=/etc/config/nfssetting

fail() { printf '%s\n' "$1" >&2; exit 1; }
physical=$(readlink -f "/share/docker/komodo/data/$name")
configured=$(/sbin/getcfg "$share" path -f /etc/config/smb.conf)
[ -n "$physical" ] && [ "$configured" = "$physical" ] || fail "Goku share path mismatch."

access=$(/sbin/getcfg Access "$physical" -f "$settings")
allowed=$(/sbin/getcfg AllowIP "$physical" -f "$settings")
[ "$allowed" = "$client" ] || fail "Goku export does not belong exclusively to the selected client."
case "$access" in TRUE|FALSE) ;; *) fail "Unknown QTS NFS access state." ;; esac

entries() {
    awk -v export_path="$export_path" -v physical="$physical" '
        { path=$1; gsub(/^"|"$/, "", path) }
        path == export_path || path == physical { print }
    ' "$1"
}

shared_entries() {
    awk -v export_path="$export_path" -v physical="$physical" '
        { path=$1; gsub(/^"|"$/, "", path) }
        path != export_path && path != physical { print }
    ' "$1" | LC_ALL=C sort
}

validate_entries() {
    if [ -z "$1" ]; then return; fi
    printf '%s\n' "$1" | awk -v client="$client" '
        { path=$1; gsub(/^"|"$/, "", path) }
        NF != 2 || index($2, client "(") != 1 || ++seen[path] > 1 { bad=1 }
        END { exit bad }
    ' || fail "Unexpected clients or aliases share the Goku export."
}

matches=$(entries /etc/exports)
validate_entries "$matches"

if [ "$mode" = preview ]; then
    if [ "$access" = FALSE ] && [ -z "$matches" ]; then printf 'absent\n'
    else printf 'eligible\n'
    fi
    exit 0
fi

[ "$mode" = apply ] && [ "$(id -u)" = 0 ] || fail "Goku apply requires an administrator."
snapshot=$(mktemp -d /tmp/onboardrr-nfs.XXXXXX)
trap 'rm -rf -- "$snapshot"' 0
shared_entries /etc/exports > "$snapshot/before"
/usr/sbin/exportfs -s > "$snapshot/active-before"
validate_entries "$(entries "$snapshot/active-before")"
shared_entries "$snapshot/active-before" > "$snapshot/shared-active-before"
if [ "$access" = FALSE ] && [ -z "$matches" ] && [ -z "$(entries "$snapshot/active-before")" ]; then
    printf 'absent\n'
    exit 0
fi

if [ "$access" = TRUE ]; then
    /sbin/setcfg Access "$physical" FALSE -f "$settings"
fi
if /etc/init.d/nfs reexport > "$snapshot/reexport" 2>&1 &&
   /usr/sbin/exportfs -s > "$snapshot/active" &&
   [ -z "$(entries /etc/exports)" ] &&
   [ -z "$(entries "$snapshot/active")" ] &&
   [ "$(/sbin/getcfg Access "$physical" -f "$settings")" = FALSE ]; then
    shared_entries /etc/exports > "$snapshot/after"
    shared_entries "$snapshot/active" > "$snapshot/shared-active-after"
    if cmp -s "$snapshot/before" "$snapshot/after" &&
       cmp -s "$snapshot/shared-active-before" "$snapshot/shared-active-after"; then
        printf 'removed\n'
        exit 0
    fi
fi

if [ "$access" = TRUE ]; then
    /sbin/setcfg Access "$physical" TRUE -f "$settings" || fail "Goku export rollback failed."
    /etc/init.d/nfs reexport > "$snapshot/rollback" 2>&1 || fail "Goku export rollback failed."
    /usr/sbin/exportfs -s > "$snapshot/active-restored" || fail "Goku export rollback failed."
    shared_entries /etc/exports > "$snapshot/restored"
    shared_entries "$snapshot/active-restored" > "$snapshot/shared-active-restored"
    [ "$(/sbin/getcfg Access "$physical" -f "$settings")" = TRUE ] &&
        [ "$(entries /etc/exports)" = "$matches" ] &&
        [ "$(entries "$snapshot/active-restored" | LC_ALL=C sort)" = "$(entries "$snapshot/active-before" | LC_ALL=C sort)" ] &&
        cmp -s "$snapshot/before" "$snapshot/restored" &&
        cmp -s "$snapshot/shared-active-before" "$snapshot/shared-active-restored" ||
        fail "Goku export rollback failed."
fi
fail "Goku export removal could not be verified."
