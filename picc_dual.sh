#!/bin/bash
# 并行截图，不保证同一帧；使用物理屏幕 ID，不是逻辑 displayId。
set -uo pipefail
serial=''; ids_arg='0,2'; auto_open=1
output="$HOME/Screencapture/$(date +%Y%m%d)"
while [ "$#" -gt 0 ]; do
    case "$1" in
        -s|-o|--display-ids)
            [ "$#" -ge 2 ] && [ -n "$2" ] || { echo '参数缺少值' >&2; exit 2; }
            case "$1" in
                -s) serial=$2;;
                -o) output=$2;;
                --display-ids) ids_arg=$2;;
            esac
            shift 2;;
        --no-open) auto_open=0; shift;;
        -h|--help)
            echo '用法: bash picc_dual.sh [-s 设备序列号] [-o 保存目录] [--display-ids ID1,ID2] [--no-open]'
            echo '默认处理所有在线设备，截取 HWC 屏幕 0 和 2，并按图片尺寸标识横屏和竖屏。'
            echo '--display-ids 支持物理 ID 或 HWC 编号，如 0,2；不支持 Android 逻辑 displayId。'
            echo '默认直接保存到 ~/Screencapture/当天日期/，文件名包含时间、设备及屏幕，不创建运行子目录。'
            echo '屏幕查询：adb shell dumpsys SurfaceFlinger --display-id'
            exit 0;;
        *) echo '未知参数，请使用 --help' >&2; exit 2;;
    esac
done
if [ -n "$ids_arg" ] && ! [[ "$ids_arg" =~ ^[0-9]+,[0-9]+$ ]]; then
    echo '需要两个逗号分隔的十进制物理屏幕 ID。' >&2; exit 2
fi
command -v adb >/dev/null || { echo '请安装 ADB 并加入 PATH。' >&2; exit 1; }
listing=$(adb devices) || exit 1
devices=$(printf '%s\n' "$listing" | awk 'NR>1 && $2=="device" {print $1}')
if [ -n "$serial" ]; then
    printf '%s\n' "$devices" | grep -Fx -- "$serial" >/dev/null || { echo '指定设备未在线或未授权。' >&2; exit 1; }
    devices=$serial
fi
[ -n "$devices" ] || { echo '没有在线设备，请检查连接与 USB 调试授权。' >&2; exit 1; }
mkdir -p "$output" || exit 1
run_dir=$output
stamp="$(date +%Y%m%d_%H%M%S)_$$_${RANDOM}"
capture() {
    local device=$1 id=$2 dir=$3
    local tmp="${dir}_display-${id}.partial"
    local signature dimensions width height direction target
    if ! adb -s "$device" exec-out screencap -p -d "$id" > "$tmp"; then
        printf '屏幕 %s 截图失败，请查看终端错误信息\n' "$id" >&2; return 1
    fi
    if [ ! -s "$tmp" ]; then
        printf '屏幕 %s 返回空数据（不是截图成功）。请检查屏幕是否启用及截图权限\n' "$id" >&2
        return 1
    fi
    signature=$(od -An -tx1 -N8 "$tmp" | tr -d ' \n')
    [ "$signature" = 89504e470d0a1a0a ] || { printf '非 PNG 输出：%s\n' "$tmp" >&2; return 1; }
    dimensions=$(od -An -tu1 -j16 -N8 "$tmp" | awk '{for(i=1;i<=NF;i++) b[++n]=$i} END {if(n==8) print b[1]*16777216+b[2]*65536+b[3]*256+b[4], b[5]*16777216+b[6]*65536+b[7]*256+b[8]}')
    read -r width height <<< "$dimensions"
    if ! [[ "$width" =~ ^[0-9]+$ && "$height" =~ ^[0-9]+$ ]] || [ "$width" -eq 0 ] || [ "$height" -eq 0 ]; then
        printf 'PNG 尺寸无效：%s\n' "$tmp" >&2; return 1
    fi
    if [ "$width" -gt "$height" ]; then direction=landscape
    elif [ "$width" -lt "$height" ]; then direction=portrait
    else direction=square
    fi
    target="${dir}_${direction}_${width}x${height}_display-${id}.png"
    mv "$tmp" "$target" || return 1
    printf '截图已保存：%s\n' "$target"
    if [ "$auto_open" -eq 1 ] && [ "$(uname -s)" = Darwin ]; then
        open "$target" || echo '自动打开失败，图片已保存。' >&2
    fi
    return 0
}
failed=0
while IFS= read -r device; do
    [ -n "$device" ] || continue
    safe=$(printf '%s' "$device" | LC_ALL=C tr -c 'A-Za-z0-9._-' '_')
    # dir 是文件名前缀，不创建运行或设备子目录。
    dir="$run_dir/${stamp}_${safe}"
    if ! displays=$(adb -s "$device" shell dumpsys SurfaceFlinger --display-id); then
        printf '设备 %s 查询屏幕失败\n' "$device" >&2; failed=1; continue
    fi
    physical_ids=$(sed -nE 's/^[[:space:]]*Display[[:space:]]+([0-9]+).*/\1/p' <<< "$displays" | awk '!seen[$0]++')
    if [ -n "$ids_arg" ]; then
        ids=''
        invalid=0
        while IFS= read -r requested; do
            if printf '%s\n' "$physical_ids" | grep -Fx -- "$requested" >/dev/null; then
                resolved=$requested
            else
                # 字符串匹配避免 awk 将 64 位物理 ID 转为浮点数而丢失精度。
                resolved=$(sed -nE "s/^[[:space:]]*Display[[:space:]]+([0-9]+)[[:space:]]+\(HWC display ${requested}\).*/\1/p" <<< "$displays" | awk '!seen[$0]++')
                if ! [[ "$resolved" =~ ^[0-9]+$ ]]; then
                    printf '设备 %s：屏幕 %s 不是有效物理 ID 或唯一 HWC 编号。可用屏幕：\n' "$device" "$requested" >&2
                    printf '%s\n' "$displays" >&2
                    invalid=1; break
                fi
                printf 'HWC 屏幕 %s → 物理 ID %s\n' "$requested" "$resolved"
            fi
            ids="${ids}${resolved}"$'\n'
        done <<< "$(printf '%s\n' "$ids_arg" | tr ',' '\n')"
        if [ "$invalid" -ne 0 ]; then failed=1; continue; fi
    else
        ids=$physical_ids
    fi
    screens=()
    while IFS= read -r id; do
        [ -n "$id" ] && screens+=("$id")
    done <<< "$ids"
    if [ "${#screens[@]}" -ne 2 ]; then
        printf '设备 %s 未识别到恰好两个物理屏幕；请用 --display-ids 指定。可用屏幕：\n%s\n' "$device" "$displays" >&2
        failed=1; continue
    fi
    if [ "${screens[0]}" = "${screens[1]}" ]; then
        echo '两个屏幕 ID 不能相同。' >&2; failed=1; continue
    fi
    printf '设备 %s：并行截取 %s 和 %s\n' "$device" "${screens[0]}" "${screens[1]}"
    capture "$device" "${screens[0]}" "$dir" &
    pid1=$!
    capture "$device" "${screens[1]}" "$dir" &
    pid2=$!
    wait "$pid1" || failed=1
    wait "$pid2" || failed=1
done <<< "$devices"
printf '保存目录：%s\n' "$run_dir"
[ "$failed" -eq 0 ] || echo '部分失败；已保留成功图片及失败的原始输出，错误信息见终端。' >&2
exit "$failed"