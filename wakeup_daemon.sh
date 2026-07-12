#!/bin/bash

# 配置
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
MGR_SCRIPT="${SCRIPT_DIR}/codex_mgr.py"
LOG_FILE="${SCRIPT_DIR}/daemon.log"

get_pid() {
    # 只取第一行，防止多个匹配结果导致 kill 失败
    pgrep -f "python3.*${MGR_SCRIPT} daemon" | head -n 1
}

start_daemon() {
    PID=$(get_pid)
    if [ -n "$PID" ]; then
        echo "提示: 后台守护进程已经在运行，PID 为 ${PID}。"
        return 0
    fi
    
    echo "正在启动 Codex 账号管理守护进程..."
    # 完全脱离终端三步走：
    #   1. stdin 重定向 /dev/null（守护进程不能读取终端输入）
    #   2. stdout/stderr 全部追加到日志文件
    #   3. disown 将其从 shell job table 中彻底移除，shell 退出时不会发 SIGHUP
    python3 -u "${MGR_SCRIPT}" daemon < /dev/null >> "${LOG_FILE}" 2>&1 &
    DAEMON_PID=$!
    disown $DAEMON_PID
    
    sleep 1.5
    PID=$(get_pid)
    if [ -n "$PID" ]; then
        echo "守护进程启动成功！"
        echo "  - PID: ${PID}"
        echo "  - 日志文件: ${LOG_FILE}"
        echo "  - 保活周期: 每半小时自动批量唤醒并更新限额缓存"
    else
        echo "守护进程启动失败，请检查日志: ${LOG_FILE}"
    fi
    return 0
}

stop_daemon() {
    PID=$(get_pid)
    if [ -z "$PID" ]; then
        echo "提示: 未发现正在运行的守护进程。"
        return 0
    fi
    
    echo "正在停止守护进程 (PID: ${PID})..."
    kill "$PID" 2>/dev/null || true
    sleep 1
    
    PID_CHECK=$(get_pid)
    if [ -z "$PID_CHECK" ]; then
        echo "守护进程已成功停止。"
    else
        echo "警告: 守护进程未响应，强行关闭中..."
        kill -9 "$PID" 2>/dev/null || true
        sleep 0.5
    fi
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
