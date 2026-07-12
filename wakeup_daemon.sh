#!/bin/bash

# 配置
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MGR_SCRIPT="${SCRIPT_DIR}/codex_mgr.py"
LOG_FILE="${SCRIPT_DIR}/daemon.log"

get_pid() {
    pgrep -f "python3.*${MGR_SCRIPT} daemon"
}

start_daemon() {
    PID=$(get_pid)
    if [ -n "$PID" ]; then
        echo "提示: 后台守护进程已经在运行，PID 为 ${PID}。"
        exit 0
    fi
    
    echo "正在启动 Codex 账号管理守护进程..."
    nohup python3 -u "${MGR_SCRIPT}" daemon > "${LOG_FILE}" 2>&1 &
    
    sleep 1.5
    PID=$(get_pid)
    if [ -n "$PID" ]; then
        echo "守护进程启动成功！"
        echo "  - PID: ${PID}"
        echo "  - 日志文件: ${LOG_FILE}"
        echo "  - 保活周期: 每 2 小时自动批量唤醒并更新限额缓存"
    else
        echo "守护进程启动失败，请检查日志: ${LOG_FILE}"
    fi
}

stop_daemon() {
    PID=$(get_pid)
    if [ -z "$PID" ]; then
        echo "提示: 未发现正在运行的守护进程。"
        exit 0
    fi
    
    echo "正在停止守护进程 (PID: ${PID})..."
    kill "${PID}"
    sleep 1
    
    PID_CHECK=$(get_pid)
    if [ -z "$PID_CHECK" ]; then
        echo "守护进程已成功停止。"
    else
        echo "警告: 守护进程未响应，强行关闭中..."
        kill -9 "${PID}"
    fi
}

check_status() {
    PID=$(get_pid)
    if [ -n "$PID" ]; then
        echo "● Codex 守护进程状态: 正在运行"
        echo "  - PID: ${PID}"
        echo "  - 运行周期: 每 2 小时"
        echo "  - 日志文件: ${LOG_FILE}"
        echo ""
        echo "最近 5 行日志:"
        tail -n 5 "${LOG_FILE}" 2>/dev/null || echo "(无日志)"
    else
        echo "○ Codex 守护进程状态: 未运行"
    fi
}

view_log() {
    if [ ! -f "${LOG_FILE}" ]; then
        echo "日志文件不存在。"
        exit 0
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
