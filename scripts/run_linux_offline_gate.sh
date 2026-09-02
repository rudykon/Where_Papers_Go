#!/usr/bin/env bash
# Run one CI gate inside a fail-closed, unprivileged Linux sandbox.

set -Eeuo pipefail

if [[ $# -eq 0 ]]; then
  echo "usage: run_linux_offline_gate.sh COMMAND [ARG ...]" >&2
  exit 2
fi
if [[ "$(/usr/bin/uname -s)" != "Linux" ]]; then
  echo "OS-level offline gates require Linux" >&2
  exit 2
fi
if [[ "$(/usr/bin/id -u)" -eq 0 ]]; then
  echo "OS-level offline gates refuse a root caller" >&2
  exit 2
fi

for required in \
  /usr/bin/find /usr/bin/getent /usr/bin/id /usr/bin/mount \
  /usr/bin/readlink /usr/bin/setpriv /usr/bin/stat /usr/bin/sudo \
  /usr/bin/uname /usr/bin/unshare /usr/sbin/ip; do
  if [[ ! -x "$required" ]]; then
    echo "OS-level offline gate is missing $required" >&2
    exit 2
  fi
done
if ! /usr/bin/sudo -n /usr/bin/true; then
  echo "OS-level offline gate requires non-interactive sudo" >&2
  exit 2
fi

script_path="$(/usr/bin/readlink -f -- "${BASH_SOURCE[0]}")"
project_root="$(/usr/bin/readlink -f -- "$(/usr/bin/dirname -- "$script_path")/..")"
if [[ ! -d "$project_root/.git" || ! -f "$project_root/.github/pr-gate-manifest.json" ]]; then
  echo "OS-level offline gate cannot identify the repository checkout" >&2
  exit 2
fi
case "$project_root/" in
  /run/*|/tmp/*|/dev/shm/*)
    echo "OS-level offline gate checkout is below a masked runtime path" >&2
    exit 2
    ;;
esac

command_name="$1"
shift
case "$command_name" in
  /*) command_path="$command_name" ;;
  */*)
    echo "OS-level offline gate requires an absolute command path" >&2
    exit 2
    ;;
  *)
    command_path="$(command -v -- "$command_name" || true)"
    ;;
esac
if [[ "$command_path" != /* || ! -x "$command_path" || "$command_path" == *$'\n'* ]]; then
  echo "OS-level offline gate command is unavailable" >&2
  exit 2
fi
case "$command_path/" in
  /run/*|/tmp/*|/dev/shm/*)
    echo "OS-level offline gate command is below a masked runtime path" >&2
    exit 2
    ;;
  "$project_root"/*)
    echo "OS-level offline gate command is below the noexec checkout" >&2
    exit 2
    ;;
esac

caller_uid="$(/usr/bin/id -u)"
caller_gid="$(/usr/bin/id -g)"
passwd_record="$(/usr/bin/getent passwd "$caller_uid")"
IFS=: read -r caller_user _ passwd_uid passwd_gid _ caller_home _ <<<"$passwd_record"
if [[ -z "$caller_user" || "$passwd_uid" != "$caller_uid" || \
      "$passwd_gid" != "$caller_gid" || "$caller_home" != /* ]]; then
  echo "OS-level offline gate cannot validate the caller identity" >&2
  exit 2
fi
if [[ ! -d "$caller_home" || -L "$caller_home" || \
      "$(/usr/bin/readlink -f -- "$caller_home")" != "$caller_home" ]]; then
  echo "OS-level offline gate rejects a redirected caller home" >&2
  exit 2
fi
host_netns_id="$(/usr/bin/stat -Lc '%d:%i' /proc/self/ns/net)"
if [[ ! "$host_netns_id" =~ ^[0-9]+:[0-9]+$ ]]; then
  echo "OS-level offline gate cannot fingerprint the host network namespace" >&2
  exit 2
fi

runner_commands_dir=/nonexistent
if [[ -n "${RUNNER_TEMP:-}" ]]; then
  candidate="${RUNNER_TEMP%/}/_runner_file_commands"
  if [[ -d "$candidate" && ! -L "$candidate" ]]; then
    candidate="$(/usr/bin/readlink -f -- "$candidate")"
    case "$candidate/" in
      "$caller_home"/*) runner_commands_dir="$candidate" ;;
      *)
        echo "OS-level offline gate rejects an unsafe runner command path" >&2
        exit 2
        ;;
    esac
  elif [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
    echo "OS-level offline gate cannot locate GitHub runner command files" >&2
    exit 2
  fi
fi

runner_tool_cache=/nonexistent
if [[ -n "${RUNNER_TOOL_CACHE:-}" ]]; then
  if [[ ! -d "$RUNNER_TOOL_CACHE" || -L "$RUNNER_TOOL_CACHE" ]]; then
    echo "OS-level offline gate rejects an unsafe runner tool cache" >&2
    exit 2
  fi
  runner_tool_cache="$(/usr/bin/readlink -f -- "$RUNNER_TOOL_CACHE")"
  if [[ "$runner_tool_cache" != /* || "$runner_tool_cache" == *$'\n'* ]]; then
    echo "OS-level offline gate rejects a redirected runner tool cache" >&2
    exit 2
  fi
elif [[ "${GITHUB_ACTIONS:-}" == "true" ]]; then
  echo "OS-level offline gate requires the GitHub runner tool cache path" >&2
  exit 2
fi

/usr/bin/sudo -n /usr/bin/env -i \
  PATH=/usr/local/sbin:/usr/local/bin:/usr/sbin:/usr/bin:/sbin:/bin \
  LANG=C.UTF-8 \
  LC_ALL=C.UTF-8 \
  HOME=/nonexistent \
  TMPDIR=/tmp \
  USER="$caller_user" \
  LOGNAME="$caller_user" \
  SHELL=/bin/bash \
  XDG_CONFIG_HOME=/nonexistent \
  XDG_CACHE_HOME=/nonexistent \
  XDG_DATA_HOME=/nonexistent \
  HF_HUB_OFFLINE=1 \
  TRANSFORMERS_OFFLINE=1 \
  HF_DATASETS_OFFLINE=1 \
  PYTHONDONTWRITEBYTECODE=1 \
  WPG_PR_OS_OFFLINE_ACTIVE=linux-sandbox-v3 \
  WPG_PR_HOST_NETNS_ID="$host_netns_id" \
  WPG_PR_CALLER_UID="$caller_uid" \
  WPG_PR_CALLER_GID="$caller_gid" \
  WPG_PR_CALLER_HOME="$caller_home" \
  WPG_PR_SANDBOX_ROOT="$project_root" \
  WPG_PR_RUNNER_COMMANDS_DIR="$runner_commands_dir" \
  WPG_PR_RUNNER_TOOL_CACHE="$runner_tool_cache" \
  /usr/bin/unshare \
    --mount \
    --net \
    --ipc \
    --uts \
    --pid \
    --fork \
    --kill-child=KILL \
    --mount-proc \
    --propagation private \
    -- \
  /bin/bash --noprofile --norc -Eeuo pipefail -c '
    # Metadata is carried by the explicit env -i block above.  Keep every
    # positional parameter reserved for the target command across both
    # bash -c boundaries.
    caller_uid="${WPG_PR_CALLER_UID:?}"
    caller_gid="${WPG_PR_CALLER_GID:?}"
    project_root="${WPG_PR_SANDBOX_ROOT:?}"
    runner_commands_dir="${WPG_PR_RUNNER_COMMANDS_DIR:?}"
    caller_home="${WPG_PR_CALLER_HOME:?}"
    runner_tool_cache="${WPG_PR_RUNNER_TOOL_CACHE:?}"

    # Drop the inherited checkout cwd before overmounting it.  Otherwise the
    # old mount remains reachable through the process's cwd despite the new
    # read-only bind at the same pathname.
    cd /

    readonly_bind() {
      local target="$1"
      /usr/bin/mount --bind "$target" "$target"
      /usr/bin/mount -o remount,bind,ro,nosuid,nodev,noexec "$target"
    }
    readonly_bind_exec() {
      local target="$1"
      /usr/bin/mount --bind "$target" "$target"
      /usr/bin/mount -o remount,bind,ro,nosuid,nodev "$target"
    }
    mask_directory() {
      local target="$1"
      local resolved
      if [[ ! -e "$target" && ! -L "$target" ]]; then
        return
      fi
      if [[ ! -d "$target" || -L "$target" ]]; then
        echo "OS-level offline gate rejects unsafe socket directory: $target" >&2
        exit 2
      fi
      resolved="$(/usr/bin/readlink -f -- "$target")"
      if [[ "$resolved" != "$target" ]]; then
        echo "OS-level offline gate rejects redirected socket directory: $target" >&2
        exit 2
      fi
      /usr/bin/mount -t tmpfs \
        -o rw,nosuid,nodev,noexec,mode=0700,size=1m \
        wpg-private "$target"
    }

    # `unshare --mount --propagation private` above already applies the
    # recursive MS_PRIVATE transition before this shell starts.  Repeating
    # that mount operation is rejected by some otherwise capable hosted
    # runners, so verify the resulting namespace below after privileges drop.
    /usr/bin/mount -t tmpfs \
      -o rw,nosuid,nodev,noexec,mode=0755,size=4m \
      wpg-run /run
    /usr/bin/mount -t tmpfs \
      -o rw,nosuid,nodev,mode=1777,size=1g \
      wpg-tmp /tmp
    /usr/bin/mount -t tmpfs \
      -o rw,nosuid,nodev,noexec,mode=1777,size=64m \
      wpg-shm /dev/shm

    # A test process must not be able to rewrite checked-out actions, runner
    # post hooks, temp command files, runner binaries, or the hosted tool cache
    # for execution after the isolated terminal step returns.
    readonly_bind_exec "$caller_home"
    if [[ "$runner_tool_cache" != /nonexistent && \
          "$runner_tool_cache" != "$caller_home" ]]; then
      readonly_bind_exec "$runner_tool_cache"
    fi

    mask_directory "$caller_home/.docker"
    mask_directory "$caller_home/.gnupg"
    mask_directory "$caller_home/.ssh"
    mask_directory "$caller_home/.local/share/containers"
    mask_directory /var/snap/lxd/common/lxd

    readonly_bind "$project_root"
    if [[ "$runner_commands_dir" != /nonexistent ]]; then
      readonly_bind "$runner_commands_dir"
    fi
    cd -- "$project_root"

    /usr/sbin/ip link set lo up
    mapfile -t interfaces < <(/usr/sbin/ip -o link show)
    [[ "${#interfaces[@]}" -eq 1 && "${interfaces[0]}" == *" lo:"* ]]
    [[ -z "$(/usr/sbin/ip -4 route show table main)" ]]
    [[ -z "$(/usr/sbin/ip -6 route show table main)" ]]

    exec /usr/bin/setpriv \
      --reuid="$caller_uid" \
      --regid="$caller_gid" \
      --clear-groups \
      --inh-caps=-all \
      --ambient-caps=-all \
      --bounding-set=-all \
      --no-new-privs \
      --pdeathsig=KILL \
      -- \
      /bin/bash --noprofile --norc -Eeuo pipefail -c '\''
        caller_uid="${WPG_PR_CALLER_UID:?}"
        caller_gid="${WPG_PR_CALLER_GID:?}"
        project_root="${WPG_PR_SANDBOX_ROOT:?}"
        runner_commands_dir="${WPG_PR_RUNNER_COMMANDS_DIR:?}"
        caller_home="${WPG_PR_CALLER_HOME:?}"
        runner_tool_cache="${WPG_PR_RUNNER_TOOL_CACHE:?}"

        uid_line=
        gid_line=
        groups_line=missing
        no_new_privs=
        cap_inh=
        cap_prm=
        cap_eff=
        cap_bnd=
        cap_amb=
        pid_line=
        while read -r key values; do
          case "$key" in
            Uid:) uid_line="$values" ;;
            Gid:) gid_line="$values" ;;
            Groups:) groups_line="$values" ;;
            NoNewPrivs:) no_new_privs="$values" ;;
            CapInh:) cap_inh="$values" ;;
            CapPrm:) cap_prm="$values" ;;
            CapEff:) cap_eff="$values" ;;
            CapBnd:) cap_bnd="$values" ;;
            CapAmb:) cap_amb="$values" ;;
            Pid:) pid_line="$values" ;;
          esac
        done </proc/self/status

        [[ "$uid_line" == "$caller_uid $caller_uid $caller_uid $caller_uid" ]]
        [[ "$gid_line" == "$caller_gid $caller_gid $caller_gid $caller_gid" ]]
        [[ -z "$groups_line" ]]
        [[ "$no_new_privs" == 1 ]]
        [[ "$pid_line" == 1 ]]
        for capability_set in \
          "$cap_inh" "$cap_prm" "$cap_eff" "$cap_bnd" "$cap_amb"; do
          [[ "$capability_set" == 0000000000000000 ]]
        done
        [[ "$(pwd -P)" == "$project_root" ]]
        while IFS= read -r mount_line; do
          case " $mount_line " in
            *" shared:"*|*" master:"*|*" propagate_from:"*)
              echo "OS-level offline gate retained propagating mounts" >&2
              exit 2
              ;;
          esac
        done </proc/self/mountinfo
        [[ ! -w . && ! -w "$project_root" ]]
        [[ ! -w "$project_root/.github/pr-gate-manifest.json" ]]
        [[ ! -w "$project_root/scripts/validate_pr_gates.py" ]]
        [[ ! -w "$project_root/scripts/run_linux_offline_gate.sh" ]]
        [[ ! -w "$project_root/uv.lock" ]]
        [[ ! -w .github/pr-gate-manifest.json ]]
        [[ ! -w scripts/validate_pr_gates.py ]]
        [[ ! -w scripts/run_linux_offline_gate.sh ]]
        [[ ! -w uv.lock ]]
        [[ ! -w "$caller_home" ]]
        if [[ "$runner_commands_dir" != /nonexistent ]]; then
          [[ ! -w "$runner_commands_dir" ]]
        fi
        if [[ "$runner_tool_cache" != /nonexistent ]]; then
          [[ ! -w "$runner_tool_cache" ]]
        fi
        [[ -z "${GITHUB_ENV+x}${GITHUB_PATH+x}${GITHUB_OUTPUT+x}" ]]
        [[ ! -S /run/docker.sock && ! -S /var/run/docker.sock ]]
        [[ ! -S /run/dbus/system_bus_socket && ! -S /run/systemd/private ]]
        [[ -z "$(/usr/bin/find /run /tmp /dev/shm -xdev -type s -print -quit)" ]]
        if /usr/bin/sudo -n /usr/bin/true >/dev/null 2>&1; then
          echo "OS-level offline gate retained sudo privilege" >&2
          exit 2
        fi
        exec "$@"
      '\'' wpg-unprivileged "$@"
  ' wpg-root "$command_path" "$@"
