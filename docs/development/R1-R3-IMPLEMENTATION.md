# InkTime R1–R3 实施规范与验收计划

> 基线：\`local-fix\` @ \`378036ee\`  
> 开发分支：\`refactor/architecture-v1\`  
> 代码评审：[Draft PR #1](https://github.com/linyubin/InkTime/pull/1)  
> 执行台账：[R1-R3-TRACKER.md](R1-R3-TRACKER.md)  
> 工作包编号 PR-00 ~ PR-10 是计划编号，并不意味着 GitHub 中已创建同名 PR；仓库 Issues 目前关闭。

## 目标与约束

保留 Python/Flask/SQLite/Pillow、ESP32 Arduino、BLE 可选路径和 480×800 / 800×480 四色 1 byte/pixel 协议；避免重新平台化。以 **保护数据 → 保证版本一致 → 确认显示事件 → 降低维护成本** 为顺序分批交付。

开发修改只进入 \`refactor/architecture-v1\`。未获得明确批准前，不得合入 \`local-fix\` 或 \`main\`、部署服务、修改真实照片库、更新实体 ESP32。每次提交必须在台账中记录：commit、代码范围、测试、未解决风险、可回滚状态。

## 工作包和验收标准

| 包 | 优先级 | 估算 | 前置 | 交付与验收 |
|---|---|---:|---|---|
| PR-00 基线/备份/CI | P0 | 0.5–1 人日 | 无 | SQLite WAL 在线备份能完整恢复；基础测试/CI 运行；固件版本可记录 |
| PR-01 NAS 扫描数据保护 | P0 | 2–3 人日 | 00 | 根目录/子目录读取失败、文件状态异常均不触发删除；缺失记录保留并可报告；明确人工清理规则 |
| PR-02 安全边界 | P0 | 2–4 人日 | 00 | 管理员身份认证、强 CSRF、防穿越、设备凭据轮换、后台 HTTPS；设备接口兼容；消除明文硬编码凭据 |
| PR-03 SQLite/配置/依赖 | P0/P1 | 1–2 人日 | 00 | 依赖可安装；schema version 增量迁移及回滚；WAL/busy_timeout 一致；Docker/systemd 配置对齐 |
| PR-04 展示事件 | P1 | 1–2 人日 | 03 | GET 不等于显示成功；started/verified/display_completed/failed 持久化；幂等 ACK；时间跨日正确 |
| PR-05 原子图片批次 | P1 | 3–5 人日 | 04 | 两种渲染入口写隔离暂存；manifest、sha256、尺寸、像素域校验；目录/指针原子切换；保留上一良好批次 |
| PR-06 ESP32 协议/ACK | P1 | 4–6 人日 | 05 | 设备固定 batch_id 下载；流式校验；坏包禁止屏幕刷新；ACK 断线重传；老固件兼容；30 次真机成功刷新 |
| PR-07 持久任务 | P1 | 2–4 人日 | 05 | cron/WebUI 共享提交入口；单工作者或可靠跨进程锁；崩溃恢复、重试与任务状态持久化 |
| PR-08 Python 模块化 | P2 | 3–5 人日 | 07 | analysis/selection/rendering/delivery 与 Flask 分离；CLI/API 兼容；固定输入回归结果一致 |
| PR-09 固件模块化 | P2 | 3–5 人日 | 06 | NVS、Wi-Fi、HTTP、Downloader、Device State Machine 独立；Flash/RAM 控制；Deep Sleep/常开回归 |
| PR-10 运维/测试/文档 | P2 | 1–3 人日 | 08,09 | job/batch/device 关联日志、健康指标、故障注入、部署/回滚文档，CI + 硬件验收完成 |

估算合计约 **21–40 人日**，取决于硬件联调时间、回归自动化基础及 NAS 环境；不是承诺完成日期。

## R1 测试矩阵

### 数据保护

- 正常遍历：所有图片都被识别；无虚假缺失。
- NAS 根目录不可读：明确报错；不覆盖有效文件快照；SQLite 分析记录未被更改。
- NAS 子目录读取失败：不把失败目录解释成空目录。
- \`DirEntry.is_file()\` / \`is_dir()\` 抛 OSError：中止而不是跳过并清理。
- 文件真实被删除：先保留数据库评分；如果需要清理，要求独立确认和备份。
- Windows UNC 与 Linux 映射路径共存：去重与缺失报告正确。

### HTTP / 身份

- 未认证访问 \`/review\`、\`/images\`、写入 API：拒绝。
- 未设置管理员密码：非设备路由默认不可用；不得默默开放。
- 设备下载路径仍通过旧版 DOWNLOAD_KEY 检查。
- Origin 不同源的写操作被拒绝；后续加入完整 CSRF token 校验。
- 绝对路径、\`../\`、同名前缀 sibling 目录、符号链接越界均拒绝。
- \`/shutdown\` 不允许通过下载密钥结束服务。
- 部署后验证管理员认证只经 HTTPS 传输，不把密码打印到日志。

### 数据库/恢复

- 在线 WAL 修改时创建 SQLite 备份；\`PRAGMA integrity_check\` = \`ok\`。
- 恢复到新的测试数据库，比较 \`photo_scores\` 计数与关键字段散列。
- 迁移脚本重复运行幂等；失败后回滚无未完成 Schema。
- 不在生产照片路径执行清理或覆盖。

## R2 测试矩阵

- 任意第 N 张渲染失败：不得更改 \`current.json\`。
- 生成两个版本时，单个设备的 JSON 与 BIN 始终固定到同一 batch_id。
- 并发 cron 和 WebUI 手动推送：只能在共享输出发布点有一个 writer。
- BIN 小于 384000、像素索引超过 3、横竖画布与元数据不符：拒绝发布。
- 校验哈希失败或 HTTP 中途断开：设备不显示半帧。
- 下载完成但设备上报失败：保持 pending，重启或下次连接补传。
- ACK 重复上报：事件主键冲突安全无副作用；同照片使用次数不重复增加。
- 日期跨日：\`used_at\` 反映实际确认日期。
- 老固件在过渡期仍可下载，但不能被误判为具备 ACK 语义。

## R3 测试矩阵

- Flask / CLI 选片及渲染固定输入输出一致。
- 端到端日志可从 \`job_id\` → \`batch_id\` → \`device_id\` 检索。
- EPD 横竖屏、舵机、休眠/唤醒、常开调试都经过真机回归。
- 对任务管理器施加 kill/restart，状态可恢复，不能重复发布损坏批次。
- 用标准化流程从零部署 Docker 或 systemd 目标环境。
- 真实显示 30 次；包括 Wi-Fi 失败、404、Hash 错误、服务器重启和消息补传。

## 发布门禁

1. **Gate R1**：所有 P0 缺陷修复经代码检查、CI、NAS 断线实验、数据库恢复演练以及后台认证/HTTPS 验证。
2. **Gate R2**：服务端先部署且新旧协议共存；设备真机成功完成下载/显示/ACK，混用多批次测试通过，失败保持上一画面。
3. **Gate R3**：兼容回归通过、部署文档可靠、固件构建可复现、日志/告警可追踪；必要时才能拆除旧协议。
4. 每个 Gate 需要人工审查签核。GitHub CI 仅覆盖其实际运行的部分，不代表上述 Gate 自动完成。

## 回滚计划

- **提交/分支**：可从 Draft PR 回退单次提交；不得强推或合并覆盖 \`local-fix\`。
- **SQLite**：使用 \`python scripts/backup_sqlite.py --database photos.db --out-dir backups\`，外部保留备份并验证恢复；增量迁移不删除旧表、旧列。
- **服务端**：保留上一已验证程序版本；恢复旧代码前确认 Schema 向后兼容。
- **批次**：不可变目录按 ID 保存；切换 \`current.json\` 指回上一个已验证批次。
- **ESP32**：保留匹配源码 SHA 的固件 BIN、Arduino Core/库版本和设备 NVS 配置；出现异常先恢复固件。
- **凭据**：任何已公开硬编码凭据都视为泄露，迁移到 NVS/设备配置并及时轮换。

## 当前真实状态

以 \`R1-R3-TRACKER.md\` 为唯一最新状态来源。已经存在的 safety tests 与 batch_storage 是初步实现；未做全量端到端测试、数据库迁移、真实 ESP32 烧录或生产部署，不应将计划工作包误认为完成。
