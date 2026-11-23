#!/system/bin/sh
# Rootless collector. State is data only: never source/eval device metadata.
ROOT=/storage/emulated/0/Download/sysmonitor_top_capture
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
archive_name() {
    ARCHIVE_NAME=raw.txt
    [ ! -L "$1/archive_name" ] || fail 'Unsafe archive name'
    if [ -e "$1/archive_name" ]; then
        [ -f "$1/archive_name" ] && [ "$(wc -c < "$1/archive_name")" -le 22 ] || fail 'Invalid archive name'
        ARCHIVE_NAME=$(read_bytes 22 "$1/archive_name") || fail 'Cannot read archive name'
        case "$ARCHIVE_NAME" in Top[0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9][0-9].txt) ;;
            *) fail 'Invalid archive name';;
        esac
    fi
}
current() {
    [ -f "$ROOT/current" ] && [ ! -L "$ROOT/current" ] || fail 'Unsafe current pointer'
    [ -r "$ROOT/current" ] || fail 'Top current pointer permission denied; preserve task data for manual inspection; state unconfirmed'
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
abort_worker() {
    # Preserve evidence and the permanent claim. No receipt, rollback or recovery.
    exit 1
}
finish() {
    [ "${INTERRUPTED:-0}" -eq 0 ] || abort_worker
    # Serialize terminal publication against recovery clear in new workers.
    commit_lock
    trap release_commit EXIT
    trap 'trap - EXIT; exit 1' HUP INT TERM
    [ ! -e "$DIR/clear_unconfirmed" ] && [ ! -L "$DIR/clear_unconfirmed" ] || abort_worker
    # Only normal cooperative completion may publish a terminal receipt.
    trap '' TERM INT
    [ ! -L "$DIR/$ARCHIVE_NAME" ] || exit 1
    truncate -s "$BYTES" "$DIR/$ARCHIVE_NAME" || exit 1
    [ "$(wc -c < "$DIR/$ARCHIVE_NAME")" -eq "$BYTES" ] || exit 1
    STATUS=$1
    ERROR=$2
    ENDED_AT=$(date +%s)
    # No archive writes follow this marker; publish it before terminal state.
    printf '%s\n' "$ID" > "$DIR/done" || exit 1
    state
    exit 0
}
# Linux uptime is independent of wall-clock corrections (10 ms resolution).
# Never fall back to date: changing the device clock must not change cadence.
monotonic_ms() {
    awk 'NR == 1 && $1 ~ /^[0-9]+[.][0-9]+$/ {
        printf "%.0f\n", $1 * 1000; valid=1; exit
    } END { if (!valid) exit 1 }' "$PROC/uptime"
}
commit_lock() {
    # Never steal a lock from an interrupted finish/clear operation.
    mkdir "$DIR/commit.busy" 2>/dev/null || fail 'Task commit is active or interrupted'
}
release_commit() {
    rmdir "$DIR/commit.busy" 2>/dev/null || :
}
release_control() {
    # Only the process that created this directory installs this trap.
    rmdir "$ROOT/control.busy" 2>/dev/null || :
}
control_busy() {
    [ -e "$ROOT/control.busy" ] || [ -L "$ROOT/control.busy" ]
}
release_snapshot() {
    rm -f "$STATE_FILE" || return 1
    rmdir "$SNAPSHOT_DIR" 2>/dev/null || :
}
control_lock() {
    # FUSE supports atomic mkdir, not flock. Never steal a stale directory:
    # an interrupted controller may still have children touching task data.
    mkdir "$ROOT/control.busy" 2>/dev/null ||
        fail 'Another device operation is active or interrupted; state unconfirmed'
    trap release_control EXIT
    # A signal may leave a foreground child alive. Keep the directory reserved.
    trap 'trap - EXIT; exit 1' HUP INT TERM
}
state_validity() (
    # 0 = valid, 1 = corrupt data, 2 = unreadable/unsafe/tool failure.
    [ -f "${STATE_FILE:-$DIR/state}" ] && [ -r "${STATE_FILE:-$DIR/state}" ] && [ ! -L "${STATE_FILE:-$DIR/state}" ] || exit 2
    SIZE=$(wc -c < "${STATE_FILE:-$DIR/state}") || exit 2
    case "$SIZE" in ''|*[!0-9[:space:]]*) exit 2;; esac
    [ "$SIZE" -le 2048 ] || exit 1
    LC_ALL=C awk -v id="$ID" -v max="$MAX_BYTES" -v size="$SIZE" '
        # Validate encoded UTF-8 without depending on locale or external decoders.
        function valid_title(s,    i,b,n,left,lo,hi) {
            if (length(s)>960 || length(s)%2 || s!~/^[0-9a-f]*$/) return 0
            n=0; left=0; lo=128; hi=191
            for (i=1; i<=length(s); i+=2) {
                b=(index("0123456789abcdef",substr(s,i,1))-1)*16 + index("0123456789abcdef",substr(s,i+1,1))-1
                if (left) {
                    if (b<lo || b>hi) return 0
                    left--; lo=128; hi=191
                } else {
                    n++
                    if (b<=127) continue
                    if (b>=194 && b<=223) left=1
                    else if (b>=224 && b<=239) {
                        left=2
                        if (b==224) lo=160
                        if (b==237) hi=159
                    } else if (b>=240 && b<=244) {
                        left=3
                        if (b==240) lo=144
                        if (b==244) hi=143
                    } else return 0
                }
            }
            return left==0 && n<=120
        }
        NR==1 && ("id:" $0)!=("id:" id) { bad=1 }
        NR==2 && $0!~/^(starting|running|stopped|completed|error)$/ { bad=1 }
        NR==3 && ($0!~/^[0-9]+$/ || length($0)>10 || $0+0>10000000) { bad=1 }
        NR==4 && ($0!~/^(-1|[0-9]+)$/ || ($0!="-1" &&
            (length($0)>6 || $0+0<1 || $0+0>100000))) { bad=1 }
        NR==5 && ($0!~/^[0-9]+([.][0-9]+)?$/ || length($0)>24 || $0+0<1 || $0+0>3600) { bad=1 }
        NR==6 && ($0!~/^[0-9]+$/ || length($0)>10 || $0+0>max) { bad=1 }
        NR==7 && $0!~/^(none|size_limit|top_failed_or_timeout|sample_limit|invalid_top|disk_error|detach_failed|interrupted|sampling_overrun|clock_failed)$/ { bad=1 }
        NR==8 && !valid_title($0) { bad=1 }
        NR>8 && ($0!~/^[0-9]+$/ || length($0)>12) { bad=1 }
        # Every record must end with LF; otherwise status would join the last
        # field and the observation timestamp. Byte accounting also catches NUL
        # bytes on awk implementations that discard them while parsing input.
        { bytes+=length($0)+1 }
        END { exit (bad || bytes!=size+0 || (NR!=8 && NR!=10)) ? 42 : 0 }
    ' "${STATE_FILE:-$DIR/state}"
    case $? in 0) exit 0;; 42) exit 1;; *) exit 2;; esac
)
validate_state() {
    state_validity || fail 'Invalid or unreadable state; no automatic recovery; preserve task data'
}
load_state() {
    validate_state
    {
        IFS= read -r STATE_ID; IFS= read -r STATUS
        IFS= read -r COUNT; IFS= read -r TARGET
        IFS= read -r INTERVAL; IFS= read -r BYTES
        IFS= read -r ERROR; IFS= read -r TITLE
        IFS= read -r STARTED_AT || STARTED_AT=0
        IFS= read -r ENDED_AT || ENDED_AT=0
    } < "${STATE_FILE:-$DIR/state}"
    archive_name "$DIR"
}
# A boot identity is kernel-provided, never inferred from wall time or PIDs.
boot_identity() (
    [ ! -L "$PROC/sys/kernel/random/boot_id" ] &&
        [ -r "$PROC/sys/kernel/random/boot_id" ] || exit 1
    VALUE=$(read_bytes 38 "$PROC/sys/kernel/random/boot_id") || exit 1
    [ ${#VALUE} -eq 36 ] || exit 1
    printf '%s\n' "$VALUE" | LC_ALL=C awk '
        /^[0-9a-f]+-[0-9a-f]+-[0-9a-f]+-[0-9a-f]+-[0-9a-f]+$/ {
            n=split($0,a,"-"); if (n==5 && length(a[1])==8 && length(a[2])==4 &&
                length(a[3])==4 && length(a[4])==4 && length(a[5])==12) ok=1
        } END { exit !ok }' || exit 1
    printf '%s\n' "$VALUE"
)
task_boot() (
    [ ! -L "$DIR/boot" ] && [ -f "$DIR/boot" ] && [ -r "$DIR/boot" ] || exit 1
    [ "$(wc -c < "$DIR/boot")" -eq 70 ] || exit 1
    { IFS= read -r OWNER; IFS= read -r BOOT; } < "$DIR/boot" || exit 1
    [ "$OWNER" = "$ID" ] || exit 1
    # Validate exactly the same canonical UUID shape as the kernel value.
    printf '%s\n' "$BOOT" | LC_ALL=C awk '
        length($0)==36 && /^[0-9a-f]+-[0-9a-f]+-[0-9a-f]+-[0-9a-f]+-[0-9a-f]+$/ {
            n=split($0,a,"-"); if (n==5 && length(a[1])==8 && length(a[2])==4 &&
                length(a[3])==4 && length(a[4])==4 && length(a[5])==12) ok=1
        } END { exit !ok }' || exit 1
    printf '%s\n' "$BOOT"
)
rebooted() (
    # New workers refuse to enter a task belonging to another boot. This proof
    # therefore excludes old writers without touching state, done or the archive.
    case "$STATUS" in starting|running) ;; *) exit 1;; esac
    SAVED_BOOT=$(task_boot) && LIVE_BOOT=$(boot_identity) || exit 1
    [ "$SAVED_BOOT" != "$LIVE_BOOT" ]
)
confirmed_inactive() {
    confirmed_terminal || rebooted
}
confirmed_terminal() {
    # Old workers do not honor commit.busy/clear_unconfirmed. Never trust a
    # receipt after recovery clear, including one published by an old worker.
    [ ! -e "$DIR/clear_unconfirmed" ] && [ ! -L "$DIR/clear_unconfirmed" ] || return 1
    case "$STATUS" in stopped|completed|error) ;; *) return 1;; esac
    [ ! -L "$DIR/done" ] && [ -f "$DIR/done" ] &&
        [ "$(read_bytes 34 "$DIR/done")" = "$ID" ]
}
# Without a worker receipt or verified boot change, orphaned children may
# still be writing. Never infer inactivity from PID absence or elapsed time.
clear_task() (
    DIR=$1; ID=${DIR##*/}; ACTION=$2
    [ -d "$DIR" ] && [ ! -L "$DIR" ] || fail 'Unsafe task directory'
    archive_name "$DIR"
    for FILE in done state cleaned "$ARCHIVE_NAME" sample header; do
        [ ! -L "$DIR/$FILE" ] || fail 'Unsafe task file'
        [ ! -e "$DIR/$FILE" ] || [ -f "$DIR/$FILE" ] || fail 'Unsafe task file'
    done
    [ -f "$DIR/state" ] && [ -r "$DIR/state" ] || fail 'Unreadable task state'
    load_state
    confirmed_inactive || fail 'Capture is not confirmed stopped'
    [ "$ACTION" = apply ] || exit 0
    for FILE in "$ARCHIVE_NAME" sample header; do
        [ ! -f "$DIR/$FILE" ] || truncate -s 0 "$DIR/$FILE" || fail 'Cannot clear Top logs'
    done
    : > "$DIR/cleaned" || fail 'Cannot mark cleared Top logs'
)
MODE=$1
shift
[ ! -L "$ROOT" ] || fail 'Unsafe capture root'
if [ -e "$ROOT" ]; then
    [ -d "$ROOT" ] && [ -r "$ROOT" ] && [ -x "$ROOT" ] ||
        fail 'Top capture directory permission denied or invalid; preserve task data for manual inspection; state unconfirmed'
fi
case "$MODE" in
start|launch)
    for cmd in nohup setsid timeout truncate awk head wc cat mv date sleep top readlink tr ls; do
        command -v "$cmd" >/dev/null 2>&1 || fail "Unsupported device: missing $cmd"
    done
    head -c 0 /dev/null >/dev/null 2>&1 ||
        command -v dd >/dev/null 2>&1 || fail 'Unsupported device: head -c or dd required'
    mkdir -p "$ROOT" || fail 'Cannot create capture root'
    control_lock
    ID=$1; TARGET=$2; INTERVAL=$3; TITLE=$4
    valid_id "$ID" || fail 'Invalid capture identity'
    case "$TARGET" in -1) ;; ''|*[!0-9]*) fail 'Invalid count';; esac
    [ "$TARGET" = -1 ] || { [ "$TARGET" -ge 1 ] && [ "$TARGET" -le 100000 ]; } || fail 'Invalid count'
    awk -v n="$INTERVAL" 'BEGIN { exit !(n ~ /^[0-9]+([.][0-9]+)?$/ && n >= 1 && n <= 3600) }' || fail 'Invalid interval'
    case "$TITLE" in *[!0-9a-f]*) fail 'Invalid title';; esac
    [ ${#TITLE} -le 960 ] || fail 'Invalid title'
    [ ! -L "$ROOT/current" ] || fail 'Unsafe current pointer'
    if [ -e "$ROOT/current" ]; then
        NEW_ID=$ID
        current
        archive_name "$DIR"
        ( load_state; confirmed_inactive ) 2>/dev/null ||
            fail 'Previous capture is not confirmed stopped'
        ID=$NEW_ID
    fi
    LIVE_BOOT=$(boot_identity) || fail 'Cannot verify device boot identity; capture not started'
    DIR=$ROOT/$ID
    mkdir "$DIR" || fail 'Capture already exists'
    # Publish immutable boot evidence before state/current or any worker exists.
    printf '%s\n%s\n' "$ID" "$LIVE_BOOT" > "$DIR/boot" || fail 'Cannot save device boot identity'
    # Verify timeout actually enforces a deadline, not just command presence.
    STARTED=$(date +%s)
    timeout -s KILL 1 sleep 3 >/dev/null 2>&1
    RC=$?
    ELAPSED=$(($(date +%s) - STARTED))
    { [ "$RC" -eq 124 ] || [ "$RC" -eq 137 ]; } &&
        [ "$ELAPSED" -ge 1 ] && [ "$ELAPSED" -le 2 ] || fail 'Unsupported timeout deadline'
    monotonic_ms >/dev/null || fail 'Unsupported monotonic clock'
    sleep 0.001 || fail 'Unsupported fractional sleep'
    # POSIX shells count 512-byte blocks, some shells use 1024: at most 8 MiB.
    (ulimit -f 8192) || fail 'Unsupported file size limit'
    COUNT=0; BYTES=0; STATUS=starting; ERROR=none
    STARTED_AT=$(date +%s); ENDED_AT=0
    printf 'Top%s.txt\n' "$(date '+%Y%m%d%H%M%S')" > "$DIR/archive_name" || fail 'Cannot save archive name'
    archive_name "$DIR"
    : > "$DIR/$ARCHIVE_NAME" || fail 'Cannot create archive'
    state
    printf '%s' "$ID" > "$ROOT/current.new" && mv "$ROOT/current.new" "$ROOT/current" || fail 'Cannot publish task'
    nohup setsid sh "$0" worker "$ID" "$TARGET" "$INTERVAL" "$TITLE" </dev/null >/dev/null 2>&1 &
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
    EXPECTED=$ID
    current
    [ "$ID" = "$EXPECTED" ] || fail 'Capture identity changed'
    load_state
    [ "$STATUS" = starting ] && [ "$COUNT" -eq 0 ] && [ "$BYTES" -eq 0 ] || fail 'Worker task is not starting'
    [ ! -e "$DIR/done" ] && [ ! -L "$DIR/done" ] || fail 'Worker already finished'
    SAVED_BOOT=$(task_boot) && LIVE_BOOT=$(boot_identity) &&
        [ "$SAVED_BOOT" = "$LIVE_BOOT" ] || fail 'Worker boot identity missing or changed'
    mkdir "$DIR/worker.claim" || fail 'Worker already claimed'
    # /proc stat fields after the comm field: state, ppid, pgrp, session.
    SESSION=$(awk '{ sub(/^.*\) /, ""); print $4 }' /proc/$$/stat)
    [ "$SESSION" = "$$" ] || fail 'Worker detach failed'
    for FD in 0 1 2; do
        [ "$(readlink /proc/$$/fd/$FD)" = /dev/null ] || fail 'Worker detach failed'
    done
    COUNT=0; BYTES=0; STATUS=running; ERROR=none
    STARTED_AT=$(date +%s); ENDED_AT=0
    archive_name "$DIR"
    trap '' HUP
    # Never finish from a trap: a foreground writer may still own raw.txt.
    # The shell waits for foreground children; stop only at normal boundaries.
    INTERRUPTED=0
    trap 'INTERRUPTED=1' TERM INT
    state
    : > "$DIR/ready"
    # Keep all deadlines relative to the first sample, never to its completion.
    PERIOD_MS=$(awk -v n="$INTERVAL" 'BEGIN { printf "%.0f\n", n * 1000 }')
    NEXT_MS=$(monotonic_ms) || finish error clock_failed
    while :; do
        [ "$INTERRUPTED" -eq 0 ] || abort_worker
        [ ! -e "$DIR/clear_unconfirmed" ] && [ ! -L "$DIR/clear_unconfirmed" ] || abort_worker
        [ ! -f "$DIR/stop" ] || finish stopped none
        while :; do
            NOW_MS=$(monotonic_ms) || finish error clock_failed
            [ "$NOW_MS" -ge "$NEXT_MS" ] && break
            # Short sleeps keep cooperative stop responsive even at 3600 s.
            WAIT=$(awk -v ms="$((NEXT_MS - NOW_MS))" 'BEGIN {
                if (ms > 1000) ms=1000; printf "%.3f", ms / 1000
            }')
            sleep "$WAIT" || { [ "$INTERRUPTED" -ne 0 ] || finish error clock_failed; }
            [ "$INTERRUPTED" -eq 0 ] || abort_worker
            [ ! -e "$DIR/clear_unconfirmed" ] && [ ! -L "$DIR/clear_unconfirmed" ] || abort_worker
            [ ! -f "$DIR/stop" ] || finish stopped none
        done
        # A whole missed period cannot be recovered by burst/duplicate samples.
        [ "$NOW_MS" -lt "$((NEXT_MS + PERIOD_MS))" ] || finish error sampling_overrun
        SAMPLE_TIME=$(date '+%Y-%m-%d %H:%M:%S') || finish error clock_failed
        NEXT_MS=$((NEXT_MS + PERIOD_MS))
        # RLIMIT_FSIZE bounds stdout+stderr even for a runaway top. timeout owns
        # its direct child (exec top), so no stale PID is ever signalled here.
        timeout -s KILL 10 sh -c 'ulimit -f 8192 || exit 1; exec top -b -n 1 -d 1' > "$DIR/sample" 2>&1
        RC=$?
        [ "$INTERRUPTED" -eq 0 ] || abort_worker
        [ ! -e "$DIR/clear_unconfirmed" ] && [ ! -L "$DIR/clear_unconfirmed" ] || abort_worker
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
        printf '========== Top 采集 #%s 时间: %s ==========\n' "$((COUNT + 1))" "$SAMPLE_TIME" > "$DIR/header" || finish error disk_error
        HEADER=$(wc -c < "$DIR/header")
        ADDED=$((HEADER + SIZE + 2))
        [ "$((BYTES + ADDED))" -le "$MAX_BYTES" ] || finish stopped size_limit
        if ! { cat "$DIR/header" "$DIR/sample" && printf '\n\n'; } >> "$DIR/$ARCHIVE_NAME"; then
            abort_worker
        fi
        [ "$INTERRUPTED" -eq 0 ] || abort_worker
        [ ! -e "$DIR/clear_unconfirmed" ] && [ ! -L "$DIR/clear_unconfirmed" ] || abort_worker
        BYTES=$((BYTES + ADDED)); COUNT=$((COUNT + 1))
        state
        [ "$INTERRUPTED" -eq 0 ] || abort_worker
        [ ! -e "$DIR/clear_unconfirmed" ] && [ ! -L "$DIR/clear_unconfirmed" ] || abort_worker
        [ "$TARGET" = -1 ] || [ "$COUNT" -lt "$TARGET" ] || finish completed none
    done
    ;;
status)
    # Observation must never own the mutation lock: an interrupted poll must
    # not permanently block stop/pull. Existing locks are observed, never stolen.
    CONTROL_BUSY=
    control_busy && CONTROL_BUSY=1
    [ ! -L "$ROOT/current" ] || fail 'Unsafe current pointer'
    if [ ! -e "$ROOT/current" ]; then
        [ -z "$CONTROL_BUSY" ] && ! control_busy ||
            fail 'Another device operation is active or interrupted; state unconfirmed'
        echo idle; exit 0
    fi
    current
    # A private bounded snapshot keeps validation and output on the same bytes.
    # On interruption leave only this unique scratch directory, not a global lock;
    # a surviving foreground reader cannot interfere with any subsequent query.
    SNAPSHOT_DIR=$(mktemp -d "$ROOT/status.XXXXXXXXXX") || fail 'Cannot create status snapshot'
    STATE_FILE=$SNAPSHOT_DIR/state
    trap release_snapshot EXIT
    trap 'trap - EXIT; exit 1' HUP INT TERM
    if ! { [ ! -L "$DIR/state" ] && [ -f "$DIR/state" ] && [ -r "$DIR/state" ] &&
        (set -C; read_bytes 2049 "$DIR/state" > "$STATE_FILE") &&
        ( validate_state ); } >/dev/null 2>&1; then
        printf 'corrupt\n%s\nunconfirmed\n' "$ID"
        if [ -f "$DIR/cleaned" ] && [ ! -L "$DIR/cleaned" ]; then echo cleaned; else echo present; fi
        if [ -e "$DIR/clear_unconfirmed" ] || [ -L "$DIR/clear_unconfirmed" ]; then echo clear_unconfirmed; fi
        if [ -n "$CONTROL_BUSY" ] || control_busy; then echo control_busy; fi
        exit 0
    fi
    load_state
    UNCONFIRMED=
    case "$STATUS" in
        stopped|completed|error) confirmed_terminal || UNCONFIRMED=1;;
    esac
    read_bytes 2048 "$STATE_FILE" || fail 'Cannot read capture state'
    date +%s
    [ "$ARCHIVE_NAME" = raw.txt ] || printf 'archive=%s\n' "$ARCHIVE_NAME"
    [ ! -f "$DIR/cleaned" ] || echo cleaned
    [ -z "$UNCONFIRMED" ] || echo unconfirmed
    rebooted && echo rebooted
    if [ -e "$DIR/clear_unconfirmed" ] || [ -L "$DIR/clear_unconfirmed" ]; then echo clear_unconfirmed; fi
    if [ -n "$CONTROL_BUSY" ] || control_busy; then echo control_busy; fi
    ;;
clear-live)
    # Explicit recovery affects only this task; no stop proof or fake receipt.
    EXPECTED=$1
    valid_id "$EXPECTED" || fail 'Invalid capture identity'
    control_lock
    current
    [ "$ID" = "$EXPECTED" ] || fail 'Capture identity changed'
    load_state
    for FILE in "$ARCHIVE_NAME" sample header clear_unconfirmed; do
        [ ! -L "$DIR/$FILE" ] || fail 'Unsafe task file'
        [ ! -e "$DIR/$FILE" ] || [ -f "$DIR/$FILE" ] || fail 'Unsafe task file'
    done
    commit_lock
    trap 'release_commit; release_control' EXIT
    # Record the destructive operation before touching logs, even on partial failure.
    : > "$DIR/clear_unconfirmed" || fail 'Cannot mark recovery clear'
    for FILE in "$ARCHIVE_NAME" sample header; do
        [ ! -f "$DIR/$FILE" ] || truncate -s 0 "$DIR/$FILE" || fail 'Cannot clear Top logs'
    done
    ;;
clear)
    EXPECTED=$1
    valid_id "$EXPECTED" || fail 'Invalid capture identity'
    control_lock
    current
    [ "$ID" = "$EXPECTED" ] || fail 'Capture identity changed'
    # Preflight all tasks, then recheck terminal states and receipts before clearing.
    # Keep control.busy throughout; permanent worker claims prevent reentry.
    for ACTION in check apply; do
        for TASK in "$ROOT"/*; do
            valid_id "${TASK##*/}" || continue
            clear_task "$TASK" "$ACTION" || fail 'Cannot safely clear Top logs'
        done
    done
    ;;
stop|pull|pull-live)
    control_lock
    EXPECTED=$1
    valid_id "$EXPECTED" || fail 'Invalid capture identity'
    current
    [ "$ID" = "$EXPECTED" ] || fail 'Capture identity changed'
    if [ "$MODE" = stop ]; then
        [ ! -L "$DIR/stop" ] || fail 'Unsafe stop marker'
        [ ! -e "$DIR/stop" ] || [ -f "$DIR/stop" ] || fail 'Unsafe stop marker'
        : > "$DIR/stop" || fail 'Cannot request stop; retry'
        # No polling, proc scan, signals or recovery; status only observes receipts.
    else
        [ ! -e "$DIR/cleaned" ] || fail 'Top logs already cleared'
        load_state
        if [ "$MODE" != pull-live ]; then
            confirmed_inactive || fail 'Capture is not confirmed stopped; retry status'
        fi
        [ ! -L "$DIR/$ARCHIVE_NAME" ] && [ -f "$DIR/$ARCHIVE_NAME" ] || fail 'Unsafe archive'
        SIZE=$(wc -c < "$DIR/$ARCHIVE_NAME") || fail 'Cannot measure archive'
        [ "$SIZE" -le "$MAX_BYTES" ] || fail 'Archive exceeds 1000000000 bytes'
        # Recovery copies only the initially measured prefix, never chases growth.
        LIMIT=$((MAX_BYTES + 1))
        [ "$MODE" != pull-live ] || LIMIT=$SIZE
        read_bytes "$LIMIT" "$DIR/$ARCHIVE_NAME" || fail 'Cannot read capture archive'
    fi
    ;;
*) fail 'Unknown operation';;
esac