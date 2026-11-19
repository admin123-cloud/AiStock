# AiStock 宿主机 Windows GUI 桥接入口 (`host_bridges/`)

本目录仅在 **Windows 宿主机** 由计划任务运行，**不打包进任何 Docker 镜像**。它们通过共享运行时目录 `F:\Aistock\runtime\` 与 Docker 容器通信：

| 入口文件 | 对应 Windows 计划任务 | 交互通道 |
| :--- | :--- | :--- |
| `tdx_postclose_client.py` | `AiStock_TDXAutoDownload` | 写入 `D:\TDX\vipdoc`（只读挂载入 `aistock-pipeline-runner`） |
| `ths_desktop_bridge.py` | `AiStock_THSDesktopBridge` | 读写 `F:\Aistock\runtime\ths_read_account\`（心跳 + 指令队列） |
| `ths_read_account_task.py` | `AiStock_THSReadAccount` / `AiStock_THSReadMultiAccount` | 写入 `F:\Aistock\runtime\ths_read_account\latest*.json` |
