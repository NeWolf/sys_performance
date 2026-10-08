#!/system/bin/sh
# Rootless collector. State is data only: never source/eval device metadata.
ROOT=/data/local/tmp/sysmonitor_top_capture
MAX_BYTES=1000000000
MAX_SAMPLE=8388608
PROC=/proc
umask 077
fail() { echo "$*" >&2; exit 1; }
valid_id() {
    [ ${#1} -eq 32 ] || return 1
    case "$1" in *[!0-9a-f]*) return 1;; esac
}
# Probe the option, not just the command: some Android head versions lack -c.
# Select before reading so a failed stream is never retried/duplicated.
read_bytes() (
    LIMIT=$1; FILE=$2
    if head -c 0 /dev/null >/dev/null 2>&1; then
        head -c "$LIMIT" "$FILE"
    else
        # Keep large archives efficient while never emitting beyond LIMIT.
        BLOCKS=$((LIMIT / 65536)); REST=$((LIMIT % 65536))
        if [ "$BLOCKS" -gt 0 ]; then
            dd if="$FILE" bs=65536 count="$BLOCKS" 2>/dev/null || exit 1
        fi
        if [ "$REST" -gt 0 ]; then
            dd if="$FILE" bs=1 skip="$((BLOCKS * 65536))" count="$REST" 2>/dev/null || exit 1
        fi
    fi
)
current() {
    [ -f "$ROOT/current" ] && [ ! -L "$ROOT/current" ] || fail 'Unsafe current pointer'
    [ -r "$ROOT/current" ] || fail 'Top current pointer permission denied; use the original capture user; state unconfirmed'
    SIZE=$(wc -c < "$ROOT/current") || fail 'Cannot read current capture identity'
    # Accept the original no-newline format and one optional LF, not a prefix.
    { [ "$SIZE" -eq 32 ] || [ "$SIZE" -eq 33 ]; } || fail 'Invalid current capture identity'
    ID=$(read_bytes 33 "$ROOT/current") || fail 'Cannot read current capture identity'
    valid_id "$ID" || fail 'Invalid current capture identity'
    DIR=$ROOT/$ID
    [ -d "$DIR" ] && [ ! -L "$DIR" ] || fail 'Unsafe task directory'
}
state() {
    printf '%s\n' "$ID" "$STATUS" "$COUNT" "$TARGET" "$INTERVAL" "$BYTES" "$ERROR" "$TITLE" "$STARTED_AT" "$ENDED_AT" > "$DIR/state.new" &&
        mv "$DIR/state.new" "$DIR/state" || exit 1
}
finish() {
    # A signal during append must never publish a partial sample.
    trap '' TERM INT
    [ ! -L "$DIR/raw.txt" ] || exit 1
    truncate -s "$BYTES" "$DIR/raw.txt" || exit 1
    [ "$(wc -c < "$DIR/raw.txt")" -eq "$BYTES" ] || exit 1
    STATUS=$1
    ERROR=$2
    ENDED_AT=$(date +%s)
    # No archive writes follow this marker; publish it before terminal state.
    printf '%s\n' "$ID" > "$DIR/done" || exit 1
    state
    exit 0
}
control_lock() {
    [ ! -L "$ROOT/control.lock" ] || fail 'Unsafe control lock'
    if [ -e "$ROOT/control.lock" ]; then
        [ -f "$ROOT/control.lock" ] || fail 'Unsafe control lock'
        [ -w "$ROOT/control.lock" ] || fail 'Top control lock permission denied; use the original capture user; state unconfirmed'
    fi
    # Check creation/open in a subshell: redirection failure on exec may kill
    # the shell outright. Never truncate or replace an existing lock inode.
    ( : >> "$ROOT/control.lock" ) || fail 'Cannot open Top control lock; check capture user and permissions; state unconfirmed'
    exec 0>> "$ROOT/control.lock" || fail 'Cannot open Top control lock; state unconfirmed'
    flock -n 0 || fail 'Another device operation is active; retry'
}
load_state() {
    [ -f "$DIR/state" ] && [ ! -L "$DIR/state" ] || fail 'Unsafe state; retry recovery'
    [ "$(wc -c < "$DIR/state")" -le 2048 ] || fail 'Invalid state; retry recovery'
    awk -v id="$ID" -v max="$MAX_BYTES" '
        NR==1 && $0!=id { bad=1 }
        NR==2 && $0!~/^(starting|running|stopped|completed|error)$/ { bad=1 }
        NR==3 && $0!~/^[0-9]+$/ { bad=1 }
        NR==4 && $0!~/^(-1|[0-9]+)$/ { bad=1 }
        NR==5 && ($0!~/^[0-9]+([.][0-9]+)?$/ || $0<1 || $0>3600) { bad=1 }
        NR==6 && ($0!~/^[0-9]+$/ || $0>max) { bad=1 }
        NR==7 && $0!~/^[a-z_]+$/ { bad=1 }
        NR==8 && ($0!~/^[0-9a-f]*$/ || length($0)>960) { bad=1 }
        NR>8 && $0!~/^[0-9]+$/ { bad=1 }
        END { exit (bad || (NR!=8 && NR!=10)) }
    ' "$DIR/state" || fail 'Invalid state; retry recovery'
    {
        IFS= read -r STATE_ID; IFS= read -r STATUS
        IFS= read -r COUNT; IFS= read -r TARGET
        IFS= read -r INTERVAL; IFS= read -r BYTES
        IFS= read -r ERROR; IFS= read -r TITLE
        IFS= read -r STARTED_AT || STARTED_AT=0
        IFS= read -r ENDED_AT || ENDED_AT=0
    } < "$DIR/state"
}
confirmed_terminal() {
    case "$STATUS" in stopped|completed|error) ;; *) return 1;; esac
    [ ! -L "$DIR/done" ] && [ -f "$DIR/done" ] &&
        [ "$(read_bytes 34 "$DIR/done")" = "$ID" ]
}
legacy_quiet() (
    # No PID is ever signalled. Hidden/inaccessible proc entries cannot prove
    # absence. Older workers have no inherited lock, so inspect open files too:
    # an orphaned cat may still append after the worker itself has disappeared.
    [ -d "$PROC/1" ] && [ -r "$PROC/mounts" ] || return 2
    GROUP_IDS=$(id -G) || return 2
    awk -v proc="$PROC" -v groups="$GROUP_IDS" '
        $2==proc && $3=="proc" {
            found=1; hidden="0"; gid=""; subset=0
            n=split($4, options, ",")
            for (i=1; i<=n; i++) {
                split(options[i], pair, "=")
                if (pair[1]=="hidepid") hidden=pair[2]
                if (pair[1]=="gid") gid=pair[2]
                if (pair[1]=="subset" && pair[2]!="all") subset=1
            }
            exempt=0
            n=split(groups, ids, /[[:space:]]+/)
            for (i=1; i<=n; i++) if (gid ~ /^[0-9]+$/ && ids[i]==gid) exempt=1
            if (subset) bad=1
            if (hidden!="0" && hidden!="off" &&
                !((hidden=="1" || hidden=="2" || hidden=="noaccess" || hidden=="invisible") && exempt)) bad=1
        }
        END { exit (!found || bad) }
    ' "$PROC/mounts" || return 2
    # Helpers are private to this subshell. comm may contain spaces and ')'.
    # 3 means a visibly vanished PID; unreadability is never absence.
    identity() {
        START=
        if ! { IFS= read -r STAT < "$PROCESS/stat"; } 2>/dev/null; then
            [ -r "$PROC" ] && [ -x "$PROC" ] && [ ! -e "$PROCESS" ] && return 3
            return 2
        fi
        case "$STAT" in "${PROCESS##*/} ("*') '*) ;; *) return 2;; esac
        REST=${STAT##*) }
        set -f
        set -- $REST
        set +f
        [ "$#" -ge 20 ] || return 2
        shift 19
        START=$1
        case "$START" in ''|*[!0-9]*) return 2;; esac
    }
    snapshot() {
        SNAP=
        for PROCESS in "$PROC"/[0-9]*; do
            identity; CHECK=$?
            [ "$CHECK" -ne 3 ] || continue
            [ "$CHECK" -eq 0 ] || return 2
            SNAP="$SNAP ${PROCESS##*/}:$START"
        done
        [ -n "$SNAP" ]
    }
    # This is bounded observation, not a lifetime lock or a stop receipt.
    # The legacy running chain has one worker shell and inherited cat writers;
    # a vanished worker can leave such children, hence the added-identity pass.
    # Assumes that known chain does not respawn or exec from unrelated tasks.
    # Arbitrary future opens/execs and processes born AND gone between snapshots
    # cannot be excluded by proc polling, however many quiet scans are repeated.
    # Keep legacy starting rejected by the caller; never manufacture a lock.
    SEEN=' '
    ROUND=0
    while [ "$ROUND" -lt 3 ]; do
        snapshot || return 2
        NEW=0
        for TOKEN in $SNAP; do
            PROCESS=$PROC/${TOKEN%%:*}
            EXPECTED=${TOKEN#*:}
            case "$SEEN" in *" $TOKEN "*) continue;; esac
            case "$SEEN" in *" ${TOKEN%%:*}:"*) return 2;; esac
            [ "$ROUND" -lt 2 ] || return 2
            NEW=1
            SEEN="$SEEN$TOKEN "
            identity; CHECK=$?
            [ "$CHECK" -ne 3 ] || continue
            [ "$CHECK" -eq 0 ] && [ "$START" = "$EXPECTED" ] || return 2
            ARGS=$(tr '\000' '\n' < "$PROCESS/cmdline" 2>/dev/null); CHECK=$?
            identity; LIVE=$?
            [ "$LIVE" -ne 3 ] || continue
            [ "$LIVE" -eq 0 ] && [ "$START" = "$EXPECTED" ] && [ "$CHECK" -eq 0 ] || return 2
            # Exact argv tokens, without two extra awk processes per PID.
            INDEX=0; SHELL=0; CONTROLLER=0; WORKER=0; PREVIOUS=
            while IFS= read -r ARG; do
                INDEX=$((INDEX + 1))
                case "$INDEX:$ARG" in 1:sh|1:/system/bin/sh) SHELL=1;; esac
                if [ "$INDEX" -eq 2 ]; then
                    HASH=${ARG#"$ROOT/control-"}; HASH=${HASH%.sh}
                    case "$HASH" in ''|*[!0-9a-f]*) ;; *)
                        [ "${#HASH}" -ne 64 ] || [ "$ARG" != "$ROOT/control-$HASH.sh" ] || CONTROLLER=1;;
                    esac
                fi
                [ "$INDEX:$ARG" != 3:worker ] || WORKER=1
                if [ "$PREVIOUS" = worker ] && [ "$ARG" = "$ID" ]; then
                    [ "$INDEX" -eq 4 ] && [ "$SHELL$CONTROLLER$WORKER" = 111 ] && return 1
                    return 2
                fi
                PREVIOUS=$ARG
            done <<EOF
$ARGS
EOF
            ATTEMPT=0
            while :; do
                [ -d "$PROCESS/fd" ] && [ -r "$PROCESS/fd" ] && [ -x "$PROCESS/fd" ] &&
                    FDS=$(ls -A "$PROCESS/fd" 2>/dev/null)
                CHECK=$?
                identity; LIVE=$?
                [ "$LIVE" -ne 3 ] || break
                [ "$LIVE" -eq 0 ] && [ "$START" = "$EXPECTED" ] && [ "$CHECK" -eq 0 ] || return 2
                set -- "$PROC/self"
                for FD in $FDS; do
                    case "$FD" in *[!0-9]*) return 2;; esac
                    set -- "$@" "$PROCESS/fd/$FD" "$PROC/self"
                done
                [ "$#" -gt 1 ] || break
                # self is a numeric delimiter after EACH fd, even when that fd
                # closes. This attributes failures without a subprocess per fd.
                LINKS=$(readlink "$@" 2>/dev/null); CHECK=$?
                identity; LIVE=$?
                [ "$LIVE" -ne 3 ] || break
                [ "$LIVE" -eq 0 ] && [ "$START" = "$EXPECTED" ] || return 2
                MARKER=; TARGET_LINK=; CLOSED=0
                shift
                while IFS= read -r LINK; do
                    if [ -z "$MARKER" ]; then
                        case "$LINK" in ''|*[!0-9]*) return 2;; esac
                        MARKER=$LINK
                    elif [ "$LINK" = "$MARKER" ]; then
                        [ "$#" -ge 2 ] || return 2
                        if [ -z "$TARGET_LINK" ]; then
                            # An existing symlink, including a dangling one, is
                            # NOT evidence of closure. Mixed EACCES/ENOENT fails.
                            [ ! -L "$1" ] && [ ! -e "$1" ] || return 2
                            CLOSED=$((CLOSED + 1))
                        fi
                        TARGET_LINK=
                        shift 2
                    else
                        [ -z "$TARGET_LINK" ] && [ "$#" -ge 2 ] || return 2
                        TARGET_LINK=$LINK
                        case "$LINK" in "$DIR/raw.txt"|"$DIR/raw.txt (deleted)"|"$DIR/sample"|"$DIR/sample (deleted)"|"$DIR/header"|"$DIR/header (deleted)") return 1;; esac
                    fi
                done <<EOF
$LINKS
EOF
                [ "$#" -eq 0 ] && [ -z "$TARGET_LINK" ] && [ -n "$MARKER" ] || return 2
                [ "$CHECK" -ne 0 ] || { [ "$CLOSED" -eq 0 ] || return 2; break; }
                # Only explicitly closed descriptors permit one local retry.
                [ "$ATTEMPT" -eq 0 ] && [ "$CLOSED" -gt 0 ] || return 2
                [ -r "$PROCESS/fd" ] && [ -x "$PROCESS/fd" ] || return 2
                ATTEMPT=1
            done
        done
        [ "$NEW" -ne 0 ] || return 0
        ROUND=$((ROUND + 1))
    done
    return 2
)
recover() (
    # The caller retains control.lock while this subshell takes the lifetime
    # lock on stdin (inherited by every writer, including append subprocesses).
    [ ! -L "$DIR/worker.lock" ] || fail 'Unsafe worker lock; retry recovery'
    if [ -e "$DIR/worker.lock" ]; then
        [ -f "$DIR/worker.lock" ] && [ ! -L "$DIR/worker.lock" ] || fail 'Unsafe worker lock; retry recovery'
        exec 0> "$DIR/worker.lock"
        flock -n 0
        RC=$?
        [ "$RC" -ne 1 ] || exit 0
        [ "$RC" -eq 0 ] || fail 'Cannot inspect worker lock; retry recovery'
    else
        # A legacy launch may still be delayed before exec of its worker.
        [ "$STATUS" != starting ] || fail 'Legacy launch is unconfirmed; inspect device and retry recovery'
        legacy_quiet
        RC=$?
        [ "$RC" -ne 1 ] || exit 0
        [ "$RC" -eq 0 ] || fail 'Cannot confirm no legacy writer; restore proc visibility and retry recovery'
    fi
    load_state
    confirmed_terminal && exit 0
    for FILE in raw.txt done state.new stop; do
        [ ! -L "$DIR/$FILE" ] || fail 'Unsafe recovery file'
        [ ! -e "$DIR/$FILE" ] || [ -f "$DIR/$FILE" ] || fail 'Unsafe recovery file'
    done
    [ -f "$DIR/raw.txt" ] || fail 'Missing archive; retry recovery'
    SIZE=$(wc -c < "$DIR/raw.txt") || fail 'Cannot inspect archive; retry recovery'
    [ "$SIZE" -ge "$BYTES" ] || fail 'Archive shorter than committed bytes; retry recovery'
    truncate -s "$BYTES" "$DIR/raw.txt" || fail 'Cannot recover archive; retry recovery'
    [ "$(wc -c < "$DIR/raw.txt")" -eq "$BYTES" ] || fail 'Cannot verify archive; retry recovery'
    STATUS=error; ERROR=interrupted
    [ ! -f "$DIR/stop" ] || { STATUS=stopped; ERROR=none; }
    ENDED_AT=$(date +%s)
    printf '%s\n' "$ID" > "$DIR/done" || fail 'Cannot publish recovery; retry'
    state
)
MODE=$1
shift
[ ! -L "$ROOT" ] || fail 'Unsafe capture root'
if [ -e "$ROOT" ]; then
    [ -d "$ROOT" ] && [ -r "$ROOT" ] && [ -x "$ROOT" ] ||
        fail 'Top capture directory permission denied or invalid; use the original capture user; state unconfirmed'
fi
case "$MODE" in
start)
    for cmd in nohup setsid timeout flock truncate awk head wc cat mv date sleep top readlink tr ls; do
        command -v "$cmd" >/dev/null 2>&1 || fail "Unsupported device: missing $cmd"
    done
    head -c 0 /dev/null >/dev/null 2>&1 ||
        command -v dd >/dev/null 2>&1 || fail 'Unsupported device: head -c or dd required'
    mkdir -p "$ROOT" || fail 'Cannot create capture root'
    # The lock is held for launch only, not during collection.
    # Android mksh may close descriptors above 2 on exec. Use stdin for the
    # launch lock; the detached worker replaces stdin with /dev/null.
    control_lock
    exec sh "$0" launch "$@"
    ;;
launch)
    ID=$1; TARGET=$2; INTERVAL=$3; TITLE=$4
    valid_id "$ID" || fail 'Invalid capture identity'
    case "$TARGET" in -1) ;; ''|*[!0-9]*) fail 'Invalid count';; esac
    [ "$TARGET" = -1 ] || { [ "$TARGET" -ge 1 ] && [ "$TARGET" -le 100000 ]; } || fail 'Invalid count'
    awk -v n="$INTERVAL" 'BEGIN { exit !(n ~ /^[0-9]+([.][0-9]+)?$/ && n >= 1 && n <= 3600) }' || fail 'Invalid interval'
    case "$TITLE" in *[!0-9a-f]*) fail 'Invalid title';; esac
    [ ${#TITLE} -le 960 ] || fail 'Invalid title'
    if [ -e "$ROOT/current" ]; then
        NEW_ID=$ID
        current
        ( load_state; confirmed_terminal ) || fail 'Previous capture is not confirmed stopped'
        ID=$NEW_ID
    fi
    DIR=$ROOT/$ID
    mkdir "$DIR" || fail 'Capture already exists'
    # Verify timeout actually enforces a deadline, not just command presence.
    STARTED=$(date +%s)
    timeout -s KILL 1 sleep 3 >/dev/null 2>&1
    RC=$?
    ELAPSED=$(($(date +%s) - STARTED))
    { [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; } &&
        [ "$ELAPSED" -ge 1 ] && [ "$ELAPSED" -le 2 ] || fail 'Unsupported timeout deadline'
    # POSIX shells count 512-byte blocks, some shells use 1024: at most 8 MiB.
    (ulimit -f 8192) || fail 'Unsupported file size limit'
    COUNT=0; BYTES=0; STATUS=starting; ERROR=none
    STARTED_AT=$(date +%s); ENDED_AT=0
    : > "$DIR/worker.lock" || fail 'Cannot create worker lock'
    : > "$DIR/raw.txt" || fail 'Cannot create archive'
    state
    printf '%s' "$ID" > "$ROOT/current.new" && mv "$ROOT/current.new" "$ROOT/current" || fail 'Cannot publish task'
    # Acquire before spawning: even a delayed child is covered when launch
    # releases control.lock. The child inherits the same locked description.
    exec 1> "$DIR/worker.lock"
    flock -n 1 || fail 'Cannot acquire workerlock'
    nohup setsid sh "$0" worker "$ID" "$TARGET" "$INTERVAL" "$TITLE" locked 0<&1 1>/dev/null 2>/dev/null &
    # A worker must prove a separate session and acknowledge startup.
    N=0
    while [ ! -f "$DIR/ready" ] && [ "$N" -lt 4 ]; do sleep 1; N=$((N + 1)); done
    [ -f "$DIR/ready" ] || fail 'Detached launch failed; inspect current task before retrying'
    ;;
worker)
    ID=$1; TARGET=$2; INTERVAL=$3; TITLE=$4
    valid_id "$ID" || fail 'Invalid capture identity'
    DIR=$ROOT/$ID
    [ -d "$DIR" ] && [ ! -L "$DIR" ] || fail 'Unsafe worker directory'
    # Only launch creates locks. Opening for reading cannot recreate a missing
    # legacy lock or truncate an existing one, even if it disappears meanwhile.
    [ -f "$DIR/worker.lock" ] && [ ! -L "$DIR/worker.lock" ] || fail 'Missing or unsafe worker lock'
    if [ "$5" != locked ]; then
        exec 0< "$DIR/worker.lock" || exit 1
    fi
    # The argument is not proof of ownership: verify the inherited open file
    # description and acquire it non-blockingly before touching any task data.
    [ "$(readlink /proc/$$/fd/0)" = "$DIR/worker.lock" ] &&
        [ "$DIR/worker.lock" -ef /proc/$$/fd/0 ] || fail 'Invalid worker lock descriptor'
    flock -n 0 || fail 'Worker already active or lock unavailable'
    [ -f "$DIR/worker.lock" ] && [ ! -L "$DIR/worker.lock" ] &&
        [ "$DIR/worker.lock" -ef /proc/$$/fd/0 ] || fail 'Worker lock changed'
    # /proc stat fields after the comm field: state, ppid, pgrp, session.
    SESSION=$(awk '{ sub(/^.*\) /, ""); print $4 }' /proc/$$/stat)
    [ "$SESSION" = "$$" ] || fail 'Worker detach failed'
    for FD in 1 2; do
        [ "$(readlink /proc/$$/fd/$FD)" = /dev/null ] || fail 'Worker detach failed'
    done
    COUNT=0; BYTES=0; STATUS=running; ERROR=none
    STARTED_AT=$(date +%s); ENDED_AT=0
    trap '' HUP
    # Never finish from a trap: a foreground writer may still own raw.txt.
    # The shell waits for foreground children; stop only at normal boundaries.
    INTERRUPTED=0
    trap 'INTERRUPTED=1' TERM INT
    state
    : > "$DIR/ready"
    while :; do
        [ "$INTERRUPTED" -eq 0 ] || finish stopped interrupted
        [ ! -f "$DIR/stop" ] || finish stopped none
        # RLIMIT_FSIZE bounds stdout+stderr even for a runaway top. timeout owns
        # its direct child (exec top), so no stale PID is ever signalled here.
        timeout -s KILL 10 sh -c 'ulimit -f 8192 || exit 1; exec top -b -n 1 -d 1' > "$DIR/sample" 2>&1
        RC=$?
        [ "$INTERRUPTED" -eq 0 ] || finish stopped interrupted
        [ "$RC" -eq 0 ] || finish error top_failed_or_timeout
        SIZE=$(wc -c < "$DIR/sample")
        [ "$SIZE" -gt 0 ] && [ "$SIZE" -le "$MAX_SAMPLE" ] || finish error sample_limit
        awk '
            /PID/ && /(%CPU|CPU%)/ && /(ARGS|CMD|COMMAND|NAME)/ {
                gsub(/[][]/, " ")
                for (i=1; i<=NF; i++) {
                    if ($i == "PID") pid=i
                    if ($i == "%CPU" || $i == "CPU%") cpu=i
                    if ($i == "RES") res=i
                    if ($i ~ /^(ARGS|CMD|COMMAND|NAME)$/) cmd=i
                }
                next
            }
            pid && cpu && res && cmd && NF >= cmd &&
                $pid ~ /^[0-9]+$/ && $cpu ~ /^[0-9]+([.][0-9]+)?%?$/ &&
                $res ~ /^[0-9]+([.][0-9]+)?[kKmMgGtT]?$/ { valid=1 }
            END { exit !valid }
        ' "$DIR/sample" || finish error invalid_top
        printf '========== Top 采集 #%s 时间: %s ==========\n' "$((COUNT + 1))" "$(date '+%Y-%m-%d %H:%M:%S')" > "$DIR/header" || finish error disk_error
        HEADER=$(wc -c < "$DIR/header")
        ADDED=$((HEADER + SIZE + 2))
        [ "$((BYTES + ADDED))" -le "$MAX_BYTES" ] || finish stopped size_limit
        if ! { cat "$DIR/header" "$DIR/sample" && printf '\n\n'; } >> "$DIR/raw.txt"; then
            [ "$INTERRUPTED" -eq 0 ] || finish stopped interrupted
            truncate -s "$BYTES" "$DIR/raw.txt" || exit 1
            finish error disk_error
        fi
        BYTES=$((BYTES + ADDED)); COUNT=$((COUNT + 1))
        state
        [ "$INTERRUPTED" -eq 0 ] || finish stopped interrupted
        [ "$TARGET" = -1 ] || [ "$COUNT" -lt "$TARGET" ] || finish completed none
        # Split long intervals so stop acknowledgement does not wait an hour.
        WHOLE=${INTERVAL%%.*}
        N=0
        while [ "$N" -lt "$WHOLE" ]; do
            [ "$INTERRUPTED" -eq 0 ] || finish stopped interrupted
            [ ! -f "$DIR/stop" ] || finish stopped none
            sleep 1
            N=$((N + 1))
        done
        case "$INTERVAL" in *.*) sleep "0.${INTERVAL#*.}";; esac
    done
    ;;
status)
    [ -e "$ROOT/current" ] || { echo idle; exit 0; }
    control_lock
    current
    load_state
    UNCONFIRMED=
    if ! confirmed_terminal; then
        recover || UNCONFIRMED=1
    fi
    read_bytes 2048 "$DIR/state" || fail 'Cannot read capture state'
    date +%s
    [ ! -f "$DIR/cleaned" ] || echo cleaned
    [ -z "$UNCONFIRMED" ] || echo unconfirmed
    ;;
clear)
    EXPECTED=$1
    valid_id "$EXPECTED" || fail 'Invalid capture identity'
    control_lock
    current
    [ "$ID" = "$EXPECTED" ] || fail 'Capture identity changed'
    # Preflight every task before touching any log. Metadata and controller
    # scripts stay in place, including current, so local archives remain usable.
    for TASK in "$ROOT"/*; do
        TASK_ID=${TASK##*/}
        valid_id "$TASK_ID" || continue
        [ -d "$TASK" ] && [ ! -L "$TASK" ] || fail 'Unsafe task directory'
        for FILE in done state cleaned raw.txt sample header; do
            [ ! -L "$TASK/$FILE" ] || fail 'Unsafe task file'
            [ ! -e "$TASK/$FILE" ] || [ -f "$TASK/$FILE" ] || fail 'Unsafe task file'
        done
        [ -f "$TASK/done" ] && [ "$(read_bytes 34 "$TASK/done")" = "$TASK_ID" ] || fail 'Capture is not stopped'
        # done precedes terminal state: require both to avoid racing finish().
        [ "$(head -n 1 "$TASK/state")" = "$TASK_ID" ] || fail 'Invalid task state'
        TERMINAL=$(head -n 2 "$TASK/state" | tail -n 1)
        case "$TERMINAL" in stopped|completed|error) ;; *) fail 'Capture is not stopped';; esac
    done
    for TASK in "$ROOT"/*; do
        valid_id "${TASK##*/}" || continue
        for FILE in raw.txt sample header; do
            [ ! -f "$TASK/$FILE" ] || truncate -s 0 "$TASK/$FILE" || fail 'Cannot clear Top logs'
        done
        : > "$TASK/cleaned" || fail 'Cannot mark cleared Top logs'
    done
    ;;
stop|pull)
    control_lock
    EXPECTED=$1
    valid_id "$EXPECTED" || fail 'Invalid capture identity'
    current
    [ "$ID" = "$EXPECTED" ] || fail 'Capture identity changed'
    if [ "$MODE" = stop ]; then
        [ ! -L "$DIR/stop" ] || fail 'Unsafe stop marker'
        [ ! -e "$DIR/stop" ] || [ -f "$DIR/stop" ] || fail 'Unsafe stop marker'
        : > "$DIR/stop" || fail 'Cannot request stop; retry'
        # No polling, proc scan or signals here; status performs recovery.
    else
        [ ! -e "$DIR/cleaned" ] || fail 'Top logs already cleared'
        load_state
        confirmed_terminal || fail 'Capture is not confirmed stopped; retry status'
        [ ! -L "$DIR/done" ] && [ -f "$DIR/done" ] &&
            [ "$(read_bytes 34 "$DIR/done")" = "$ID" ] || fail 'Capture is not stopped'
        [ ! -L "$DIR/raw.txt" ] || fail 'Unsafe archive'
        SIZE=$(wc -c < "$DIR/raw.txt")
        [ "$SIZE" -le "$MAX_BYTES" ] || fail 'Archive exceeds 1000000000 bytes'
        read_bytes "$((MAX_BYTES + 1))" "$DIR/raw.txt" || fail 'Cannot read capture archive'
    fi
    ;;
*) fail 'Unknown operation';;
esac