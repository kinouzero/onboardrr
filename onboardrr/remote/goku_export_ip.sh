#!/bin/sh
# Update the client on an existing QTS export and leave other exports intact.
set -eu

name=$1
kind=$2
old=$3
new=$4
config=/etc/config/nfssetting
base=$(readlink -f /share/docker)
[ -d "$base/komodo/data/$name" ] || exit 13
case "$kind" in
    dedicated) path="$base/komodo/data/$name"; alias="/share/NFSv=4/onboardrr-$name" ;;
    legacy) path="$base"; alias=/share/NFSv=4/docker ;;
    *) exit 13 ;;
esac
[ "$(/sbin/getcfg Access "$path" -f "$config")" = TRUE ] || exit 13
[ "$(stat -c '%d:%i' "$path")" = "$(stat -c '%d:%i' "$alias")" ] || exit 13
lock="$base/komodo/data/.onboardrr-nfs.lock"
mkdir -m 0700 "$lock"
rollback=0
cleanup() {
    status=$?
    trap - EXIT
    if [ "$rollback" = 1 ]; then
        /sbin/setcfg AllowIP "$path" "$before" -f "$config" &&
            /etc/init.d/nfs reexport > "$lock/rollback" 2>&1 || status=14
    fi
    rm -rf "$lock"
    exit "$status"
}
trap cleanup EXIT

other_exports() {
    awk -v path="$path" -v alias="$alias" '
        { target=$1; gsub(/^"|"$/, "", target) }
        target!=path && target!=alias
    ' "$1" | LC_ALL=C sort
}

verify_exports() {
    awk -v path="$path" -v alias="$alias" -v ip="$new" -v retired="${2:-}" '
        { target=$1; gsub(/^"|"$/, "", target) }
        target==path || target==alias {
            for (i=2; i<=NF; i++) {
                if (index($i, ip "(") == 1) found[target]=1
                if (retired != "" && index($i, retired "(") == 1) stale=1
            }
        }
        END { exit (stale || !found[path] || !found[alias]) }
    ' "$1"
}

before=$(/sbin/getcfg AllowIP "$path" -f "$config")
[ "$kind" != dedicated ] || [ "$before" = "$old" ] || [ "$before" = "$new" ] || exit 13
case ",$before," in
    *",$new,"*)
        /usr/sbin/exportfs -s > "$lock/active"
        verify_exports /etc/exports && verify_exports "$lock/active" || exit 13
        printf 'ready\n'
        exit 0 ;;
esac
[ -n "$old" ] || exit 13
after=$(printf '%s\n' "$before" | awk -F, -v old="$old" -v new="$new" '
    { for (i=1; i<=NF; i++) if ($i == old) { $i=new; found++ }
      if (found != 1) exit 1
      OFS=","; $1=$1; print }
') || exit 13
other_exports /etc/exports > "$lock/before"
/usr/sbin/exportfs -s > "$lock/active-before"
other_exports "$lock/active-before" > "$lock/other-active-before"
rollback=1
/sbin/setcfg AllowIP "$path" "$after" -f "$config"
/etc/init.d/nfs reexport > "$lock/reexport" 2>&1
/usr/sbin/exportfs -s > "$lock/active"
verify_exports /etc/exports "$old"
verify_exports "$lock/active" "$old"
[ "$(/sbin/getcfg AllowIP "$path" -f "$config")" = "$after" ]
other_exports /etc/exports > "$lock/after"
other_exports "$lock/active" > "$lock/other-active-after"
cmp -s "$lock/before" "$lock/after"
cmp -s "$lock/other-active-before" "$lock/other-active-after"
rollback=0
printf 'updated\n'
