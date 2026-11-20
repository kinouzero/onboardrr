set -eu

action=$1
name=$2
root=/share/compose
target="$root/$name"
compose=docker-compose.komodo.yml
template=template.env

case "$action" in
    reference)
        path="$root/naruto/$compose"
        [ ! -L "$root/naruto" ] && [ ! -L "$path" ] && [ -f "$path" ] &&
            [ "$(wc -c < "$path")" -le 1048576 ] || exit 1
        exec cat "$path"
        ;;
    verify|verify-local|stage) ;;
    *) exit 1 ;;
esac

[ -d "$root" ] && [ ! -L "$target" ] || exit 1
if [ "$action" != stage ]; then
    for file in "$compose" "$template"; do
        path="$target/$file"
        [ ! -L "$path" ] && [ -f "$path" ] &&
            [ "$(wc -c < "$path")" -le 1048576 ] || exit 12
    done
    data="/share/docker/komodo/data/$name"
    [ -d "$data" ] && [ ! -L "$data" ] || exit 11
    exec tar -cf - -C "$target" "$compose" "$template"
fi

umask 077
tmp=$(mktemp -d "$root/.onboardrr-periphery.XXXXXX")
trap 'rm -f "$tmp/$compose" "$tmp/$template"; rmdir "$tmp"' EXIT
tar -xf - -C "$tmp"
for file in "$compose" "$template"; do
    path="$target/$file"
    [ ! -L "$path" ] || exit 1
    if [ -e "$path" ]; then
        [ -f "$path" ] && cmp -s "$tmp/$file" "$path" || exit 1
    fi
done
mkdir -p "$target"
[ ! -L "$target" ] || exit 1
for file in "$compose" "$template"; do
    path="$target/$file"
    if [ ! -e "$path" ]; then
        [ ! -L "$path" ] && ln "$tmp/$file" "$path"
        [ ! -L "$path" ] && [ -f "$path" ] && cmp -s "$tmp/$file" "$path" || exit 1
        printf '%s\n' "$file"
    fi
done
