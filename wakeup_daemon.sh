#!/bin/bash

# 配置
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MGR_SCRIPT="${SCRIPT_DIR}/codex_mgr.py"
LOG_FILE="${SCRIPT_DIR}/daemon.log"
CODEX_HOME="${HOME}/.codex"
PID_FILE="${CODEX_HOME}/codex_mgr_daemon.pid"
CAFFEINATE_PID_FILE="${CODEX_HOME}/codex_mgr_caffeinate.pid"
# 当前机器使用 TUN：忽略 HTTP(S)_PROXY，避免应用层二次代理。
# 若关闭 TUN、改用传统本地代理，可在启动前设置 CODEX_MGR_NETWORK_MODE=env。
NETWORK_MODE="${CODEX_MGR_NETWORK_MODE:-tun}"
umask 077

get_pid() {
    [ -f "${PID_FILE}" ] || return 1
    PID=$(tr -dc '0-9' < "${PID_FILE}")
    [ -n "${PID}" ] || return 1
    if ! kill -0 "${PID}" 2>/dev/null; then
        rm -f "${PID_FILE}"
        return 1
    fi
    COMMAND=$(ps -p "${PID}" -o command= 2>/dev/null)
    case "${COMMAND}" in
        *"${MGR_SCRIPT}"*" daemon"*) echo "${PID}"; return 0 ;;
        *) rm -f "${PID_FILE}"; return 1 ;;
    esac
}

stop_caffeinate() {
    if [ -f "${CAFFEINATE_PID_FILE}" ]; then
        CAFFEINATE_PID=$(tr -dc '0-9' < "${CAFFEINATE_PID_FILE}")
        if [ -n "${CAFFEINATE_PID}" ]; then
            CAFFEINATE_COMMAND=$(ps -p "${CAFFEINATE_PID}" -o command= 2>/dev/null)
            case "${CAFFEINATE_COMMAND}" in
                *caffeinate*) kill "${CAFFEINATE_PID}" 2>/dev/null || true ;;
            esac
        fi
        rm -f "${CAFFEINATE_PID_FILE}"
    fi
}

start_daemon() {
    PID=$(get_pid)
    if [ -n "$PID" ]; then
        echo "提示: 后台守护进程已经在运行，PID 为 ${PID}。"
        return 0
    fi
    
    echo "正在启动 Codex 账号管理守护进程..."
    mkdir -p "${CODEX_HOME}"

    # 简单日志轮转，避免常驻服务无限增长。
    if [ -f "${LOG_FILE}" ] && [ "$(stat -f%z "${LOG_FILE}" 2>/dev/null || echo 0)" -gt 5242880 ]; then
        mv -f "${LOG_FILE}" "${LOG_FILE}.1"
    fi

    CODEX_MGR_NETWORK_MODE="${NETWORK_MODE}" nohup python3 -u "${MGR_SCRIPT}" daemon < /dev/null >> "${LOG_FILE}" 2>&1 &
    START_PID=$!

    # 等待 Python 持有单例锁并原子写入真实 PID 文件。
    for _ in $(seq 1 30); do
        PID=$(get_pid)
        [ -n "${PID}" ] && break
        kill -0 "${START_PID}" 2>/dev/null || break
        sleep 0.2
    done

    PID=$(get_pid)
    if [ -n "$PID" ]; then
        echo "守护进程启动成功！"
        echo "  - PID: ${PID}"
        echo "  - 日志文件: ${LOG_FILE}"
        echo "  - 保活周期: 每半小时自动批量唤醒并更新限额缓存"
        echo "  - 网络模式: ${NETWORK_MODE}"
    else
        kill "${START_PID}" 2>/dev/null || true
        echo "守护进程启动失败，请检查日志: ${LOG_FILE}"
    fi
    return 0
}

stop_daemon() {
    PID=$(get_pid)
    if [ -z "$PID" ]; then
        stop_caffeinate
        echo "提示: 未发现正在运行的守护进程。"
        return 0
    fi
    
    echo "正在停止守护进程 (PID: ${PID})..."
    kill "$PID" 2>/dev/null || true
    for _ in $(seq 1 30); do
        kill -0 "$PID" 2>/dev/null || break
        sleep 0.2
    done

    PID_CHECK=$(get_pid || true)
    if [ -z "$PID_CHECK" ]; then
        echo "守护进程已成功停止。"
    else
        echo "警告: 守护进程未响应，强行关闭中..."
        kill -9 "$PID" 2>/dev/null || true
        sleep 0.5
    fi
    rm -f "${PID_FILE}"
    stop_caffeinate
    return 0
}

check_status() {
    PID=$(get_pid)
    if [ -n "$PID" ]; then
        echo "● Codex 守护进程状态: 正在运行"
        echo "  - PID: ${PID}"
        echo "  - 运行周期: 每半小时"
        echo "  - 日志文件: ${LOG_FILE}"
        echo ""
        echo "最近一轮保活日志（末尾 40 行）:"
        echo "----------------------------------------"
        tail -n 40 "${LOG_FILE}" 2>/dev/null || echo "(无日志)"
        echo "----------------------------------------"
    else
        echo "○ Codex 守护进程状态: 未运行"
    fi
    return 0
}

view_log() {
    if [ ! -f "${LOG_FILE}" ]; then
        echo "日志文件不存在。"
        return 0
    fi
    echo "正在实时查看日志，按 Ctrl+C 退出..."
    tail -f "${LOG_FILE}"
}

print_help() {
    echo "ChatGPT/Codex 后台保活守护脚本"
    echo ""
    echo "使用方法:"
    echo "  $0 start    启动后台守护服务 (常驻后台，定期刷新 Token)"
    echo "  $0 stop     停止后台守护服务"
    echo "  $0 restart  重启后台守护服务"
    echo "  $0 status   查看守护服务运行状态"
    echo "  $0 log      实时查看守护服务的输出日志"
}

case "$1" in
    start)
        start_daemon
        ;;
    stop)
        stop_daemon
        ;;
    restart)
        stop_daemon
        start_daemon
        ;;
    status)
        check_status
        ;;
    log)
        view_log
        ;;
    *)
        print_help
        ;;
esac

exit 0
