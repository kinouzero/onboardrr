#!/bin/sh
# Prepare the device's dedicated QTS NFSv4 export.
set -eu

name=$1
client=$2
uid=$3
gid=$4
share="onboardrr-$name"
alias="/share/NFSv=4/$share"
smb=/etc/config/smb.conf
nfs=/etc/config/nfssetting

[ "$(id -u)" = 0 ]
base=$(readlink -f /share/docker/komodo/data)
[ -n "$base" ] && [ -d "$base" ] || exit 1
physical="$base/$name"
if [ "${5:-}" = local ]; then
    [ ! -L "$physical" ] || exit 13
    if [ -d "$physical" ]; then
        printf 'ready\n'
        exit 0
    fi
    [ ! -e "$physical" ] || exit 13
    mkdir -m 0770 "$physical"
    chown "$uid:$gid" "$physical"
    printf 'created-directory\n'
    exit 0
fi
[ -r "$smb" ] && [ -r "$nfs" ] || exit 1
lock="$base/.onboardrr-nfs.lock"
mkdir -m 0700 "$lock"
rollback=0
cleanup() {
    status=$?
    trap - EXIT
    if [ "$rollback" = 1 ]; then
        /sbin/setcfg Access "$physical" FALSE -f "$nfs" &&
            /etc/init.d/nfs reexport > "$lock/rollback" 2>&1 || status=14
    fi
    rm -rf "$lock"
    exit "$status"
}
trap cleanup EXIT

select_exports() {
    awk -v path="$physical" -v alias="$alias" -v mode="$1" '
        { target=$1; gsub(/^"|"$/, "", target) }
        ((target == path || target == alias) == (mode == "target")) { print }
    ' "$2" | LC_ALL=C sort
}

verify_exports() {
    awk -v path="$physical" -v alias="$alias" -v client="$client" -v uid="$uid" -v gid="$gid" '
        { target=$1; gsub(/^"|"$/, "", target) }
        target == path || target == alias {
            seen[target]++
            if (NF != 2 || index($2, client "(") != 1 || $2 !~ /[(,]rw[,)]/ ||
                $2 !~ /[(,]all_squash[,)]/ || $2 !~ ("[(,]anonuid=" uid "[,)]") ||
                $2 !~ ("[(,]anongid=" gid "[,)]")) bad=1
        }
        END { exit (bad || seen[path] != 1 || seen[alias] != 1) }
    ' "$1"
}

same_backing_directory() {
    [ ! -L "$alias" ] && [ -d "$alias" ] &&
        [ "$(stat -c '%d:%i' "$alias")" = "$(stat -c '%d:%i' "$physical")" ]
}

read_optional_setting() {
    code=0
    value=$(/sbin/getcfg "$@") || code=$?
    case "$code" in
        0) printf '%s\n' "$value" ;;
        250) [ -z "$value" ] ;;  # QTS returns 250 for a missing field.
        *) return "$code" ;;
    esac
}

[ ! -L "$physical" ] && [ ! -L "$alias" ] || exit 13
configured=$(read_optional_setting "$share" path -f "$smb")
access=$(read_optional_setting Access "$physical" -f "$nfs")
allowed=$(read_optional_setting AllowIP "$physical" -f "$nfs")
[ -z "$configured" ] || [ "$configured" = "$physical" ] || exit 13
[ -z "$allowed" ] || [ "$allowed" = "$client" ] || exit 13
case "$access" in TRUE|FALSE|'') ;; *) exit 13 ;; esac

if [ -e "$physical" ]; then
    [ -d "$physical" ] || exit 13
    uid=$(stat -c '%u' "$physical")
    gid=$(stat -c '%g' "$physical")
fi
[ "$uid" -gt 0 ] || exit 13
/usr/sbin/exportfs -s > "$lock/active"
select_exports target /etc/exports > "$lock/generated-target"
select_exports target "$lock/active" > "$lock/active-target"
if [ -s "$lock/generated-target" ] || [ -s "$lock/active-target" ]; then
    [ "$configured" = "$physical" ] && [ "$access" = TRUE ] && [ "$allowed" = "$client" ] &&
        same_backing_directory && verify_exports /etc/exports && verify_exports "$lock/active" || exit 13
    printf 'ready\n'
    exit 0
fi
[ "$access" != TRUE ] || exit 13
if [ -e "$alias" ]; then
    [ "$configured" = "$physical" ] && same_backing_directory || exit 13
fi
select_exports other /etc/exports > "$lock/generated-before"
select_exports other "$lock/active" > "$lock/active-before"
outcome=ready
if [ ! -d "$physical" ]; then
    mkdir -m 0770 "$physical"
    chown "$uid:$gid" "$physical"
    outcome=created-directory
fi
if [ -z "$configured" ]; then
    /sbin/addshare "$share" "$physical" -ar -aw > "$lock/addshare" 2>&1
    [ "$(/sbin/getcfg "$share" path -f "$smb")" = "$physical" ]
    /sbin/setcfg "$share" "guest ok" no -f "$smb"
    /sbin/setcfg "$share" browseable no -f "$smb"
    /sbin/setcfg "$share" public no -f "$smb"
fi

while read -r key value; do
    /sbin/setcfg "$key" "$physical" "$value" -f "$nfs"
done <<EOF
Is_modified 0
Auth_sys 1
Auth_krb5 0
Auth_krb5i 0
Auth_krb5p 0
Permission rw
Sync_option 1
Wdelay_option 1
Secure_option 1
SquashOption all_squash
AnonUID $uid
AnonGID $gid
AllowIP $client
EOF
rollback=1
/sbin/setcfg Access "$physical" TRUE -f "$nfs"
/etc/init.d/nfs reexport > "$lock/reexport" 2>&1
/usr/sbin/exportfs -s > "$lock/active"
verify_exports /etc/exports
verify_exports "$lock/active"
same_backing_directory
select_exports other /etc/exports > "$lock/generated-after"
select_exports other "$lock/active" > "$lock/active-after"
cmp -s "$lock/generated-before" "$lock/generated-after"
cmp -s "$lock/active-before" "$lock/active-after"
rollback=0
printf '%s\n' "$outcome"
