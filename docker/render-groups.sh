#!/usr/bin/env bash
# Give the app's user access to the host's GPU nodes (dev/changelog/1125).
#
# Sourced by entrypoint.sh, which calls join_render_groups before dropping root. A GPU
# reaches the container only when /dev/dri is passed as a device, and its nodes keep the
# HOST's ownership: 0660 root:render or root:video on most distributions (the gid differs
# per host), 0777 on Unraid. The app's user is never in that group, so without this step
# every VAAPI encode is refused with a permission error on every host but Unraid.
#
# Every node under the directory is considered, not only renderD*: some drivers expose a
# card node alone, and a group joined for a node the encoder never opens costs nothing.
# Skipped: the root group (joining it would grant far more than the GPU) and any group the
# user already holds. Idempotent - it runs on every start.
#
# Every external command is resolved through PATH so a test can stand in for stat,
# getent, groupadd, usermod and id without root or a real device.
join_render_groups() {
    local dri=${1:-/dev/dri} user=${2:-channelbin}
    local dev gid name held
    [ -d "$dri" ] || return 0
    held=" $(id -G "$user") "
    for dev in "$dri"/*; do
        [ -e "$dev" ] || continue
        [ -d "$dev" ] && continue
        gid=$(stat -c %g "$dev")
        if [ "$gid" = "0" ]; then
            log "not joining group 0 for $dev - pass a render node owned by a non-root group"
            continue
        fi
        case "$held" in *" $gid "*) continue ;; esac
        name=$(getent group "$gid" | cut -d: -f1 || true)
        if [ -z "$name" ]; then
            name="hostgpu$gid"
            groupadd -o -g "$gid" "$name"
        fi
        usermod -aG "$name" "$user"
        held="$held$gid "
        log "gave $user access to $dev (group $gid, $name)"
    done
}
