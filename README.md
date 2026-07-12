# ChatGPT/Codex 多账号管理与用量查询方案

本目录主要用于承接 macOS 下新版 ChatGPT 客户端（包含 Codex 功能）及 Codex CLI 的多账号管理、用量额度展示及自动会话保活工作。

通过本方案，您可以实现多账号**静默无感现查额度**，并在账号额度用尽时**一键快速切换**，且**完全不打扰您当前的客户端对话与写代码工作**。

---

## 📂 项目文件清单

1.  **`codex_mgr.py`**：核心 Python 管理 CLI。
    *   `list`：本地解码各账号的凭证（`auth.json`），极其直观地列出所有 Profile 的绑定邮箱、计划类型（Plus/Free）、到期日及限额使用情况。
    *   `list --refresh` (或 `-r`)：以无头静默模式在后台拉起客户端（互不冲突且无弹窗），100% 绕过 Cloudflare 阻断，在线现查并更新缓存的最实时额度。
    *   `add <name>`：将您当前的登录态备份另存为一个全新的 Profile 账号。
    *   `switch <name>`：一键备份当前账号，无缝切换到目标账号并重新打开客户端。
    *   `wakeup`：批量顺序静默刷新并更新所有账号的会话 Token。
    *   `daemon`：常驻后台定时执行唤醒。
2.  **`wakeup_daemon.sh`**：守护进程控制脚本。
    *   `./wakeup_daemon.sh start`：启动常驻后台守护服务，每 2 小时自动批量刷新保活并更新额度缓存。
    *   `./wakeup_daemon.sh stop`：停止后台守护服务。
    *   `./wakeup_daemon.sh status`：查看运行状态及最近日志。
    *   `./wakeup_daemon.sh log`：实时查看输出日志。

---

## ⚙️ 账号初始化与切换

### 1. 建立初始 Profile 备份
在您准备好的多个账号之间建立第一次备份：

1.  **配置第一个账号**
    登录您的第一个 ChatGPT 账号，然后运行以下命令保存为 `account1`：
    ```bash
    python3 codex_mgr.py add account1
    ```
2.  **配置第二个账号**
    *   在打开的 ChatGPT 客户端中，点击左下角头像选择 **Log out**（注销）。
    *   重新走一遍登录验证流程，成功进入您的 **第二个 ChatGPT 账号**。
    *   登录成功后，在终端运行以下命令保存：
      ```bash
      python3 codex_mgr.py add account2
      ```
    *(如果有更多账号，请重复此“注销 -> 登录 -> 运行 add 备份”的操作)*

---

## 🚀 常用操作

### 1. 查看账号列表与额度
*   **快速查看** (毫秒级响应，读取本地缓存)：
    ```bash
    python3 codex_mgr.py list
    ```
*   **手动在线现查** (等待约 3~5 秒，后台静默拉起无头实例，最新最准)：
    ```bash
    python3 codex_mgr.py list --refresh
    ```

### 2. 手动切换账号
直接运行以下命令即可无缝换号（自动备份当前，加载目标并重启客户端）：
```bash
python3 codex_mgr.py switch <Profile名称>
```

### 3. 常驻后台自动保活防掉登
为了防止账号长时间闲置导致 Token 被服务器主动失效，开启后台静默守护：
*   **启动服务**：
    ```bash
    ./wakeup_daemon.sh start
    ```
*   **查看状态**：
    ```bash
    ./wakeup_daemon.sh status
    ```

---

## 🔒 账号防掉登保活优化指南

OpenAI 拥有极为严格的 Cloudflare 节点风控，容易因为 IP 剧烈跳变强制将会话失效。
> [!TIP]
> **锁定代理节点出口**：
> 建议在您的代理工具（如 Clash / Surge / Shadowrocket）的**路由规则 (Rules)** 中，将 `openai.com` 和 `chatgpt.com` 的域名锁定到**相对固定的静态 IP 节点**或专门的专线节点，避免使用代理组的负载均衡自动轮询。这能大幅提升本地 Token 的存活时长，甚至做到持久在线。
