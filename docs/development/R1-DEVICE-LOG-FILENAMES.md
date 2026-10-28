# R1 / PR-02 — 设备日志文件名去密钥化（仅开发分支）

## 背景和实现

旧版 `server.py` 将下载密钥直接写进 `logs/device/<key>.log` 文件名。目录清单、备份、诊断包可能因此泄露长期有效的 bearer token。

新版只把**新上传**写入 `logs/device/device-<sha256(key)>.log`。完整 SHA-256 摘要是稳定文件标识，**不是加密或密钥保护**：弱口令仍可被离线猜测。历史暴露的下载密钥仍须轮换。

- 设备 POST 路由和 JSONL 格式保持不变，不需要修改 ESP32 协议。
- 原有 `<key>.log` 文件只读保留；不会自动移动、合并、覆盖或删除。新的写入只发生在哈希文件名。
- 服务器重启后，从同一 key 的旧、新日志文件末尾各 500 行恢复最大 `ls`，避免迁移边界的重复上传。
- `/devlog` 同时读取当前和显式配置的旧下载密钥（`INKTIME_DOWNLOAD_KEY_PREVIOUS`）对应的两种日志文件。旧密钥被撤销后，其历史文件不会再自动显示；应根据保留政策人工归档。
- 不同 key 的日志仍分开维护。没有实现设备级强身份认证、跨进程文件锁或多设备共享 key 的可靠去重。

## 隔离验收

```bash
python -m unittest discover -s tests -p 'test_*.py' -v
```

新增的 `tests/test_device_log_storage.py` 使用临时目录和从真实服务端、WebUI 源码提取的函数，验证：新文件名不含明文密钥、拒绝路径穿越、旧日志不再被追加、重启去重、旧/新密钥切换时的后台读取。

CI 只验证 Python/Flask 模拟环境。**未验证**真实 NAS 的权限/umask、文件系统异常、HTTPS 浏览器、ESP32 设备日志上传或固件构建；也未修改生产数据。

## 风险和回滚

SHA-256 文件名仍可能泄露低熵 key 的离线校验线索。旧明文文件及历史备份不会自动清理。HTTP URL、代理访问日志、日志正文、文件权限、并发写入和设备身份是独立待办项。任何真实日志迁移、删除或归档前，必须先备份并获得人工批准。

只允许在 `refactor/architecture-v1` 上逆序回退实现提交 `dd2bae00`、`427dbb3d`、`22f6864f` 并重跑 CI。**回退会恢复明文密钥文件名，不是安全的生产回滚方案。** R1 Gate 仍待真实 NAS 恢复、HTTPS 浏览器、Docker/systemd、ESP32 硬件及人工验收；R2/R3 不得宣称通过。


## 2026-10-28 — 私有日志追加与轮转（源码/CI 阶段）

- 新上传日志文件以 `0600` 权限创建，新建的 `logs/device` 目录请求 `0700`。已有的普通单链接日志文件若权限过宽，会在追加前尝试收紧至仅属主可访问；失败则 HTTP 500，不推进服务端去重游标。
- 新写入通过 `O_NOFOLLOW` 打开，拒绝符号链接、硬链接及非普通文件；符号链接的设备日志目录和不安全的轮转目标 `.log.1` 也会被拒绝。无 `O_NOFOLLOW` 的平台不会降级为不安全打开。
- **不会**自动修改已有目录权限、旧明文键名文件或历史备份；不能由此推断整个日志目录已经私有。升级前应在隔离副本检查目录/文件属主、模式及已有符号链接，确认服务账户具备收紧权限的能力。
- `tests/test_device_log_storage.py` 新增五项隔离文件系统测试；Push CI [40671934019](https://github.com/linyubin/InkTime/actions/runs/40671934019)、PR CI [40671942657](https://github.com/linyubin/InkTime/actions/runs/40671942657) 均通过，148 Python + 4 Node。**未做真实 NAS、HTTPS 浏览器或 ESP32 验收。**
- 仍存在多进程轮转竞争、崩溃前未 fsync、旧日志/WebUI 读取路径可能跟随符号链接、设备身份和 HTTP bearer token 暴露问题。真实 NAS 生产数据不得用于本轮试验。
- 回滚仅限开发分支 `git revert ca180e60` 并重新运行 CI；此操作会重新引入不安全的追加方式，**不是可直接部署的生产回滚方案**。R1 Gate 继续 OPEN。

## 2026-10-28 — 历史日志读取防符号链接加固（开发分支）

- `device_log_storage.read_device_log_lines()` 以 `O_DIRECTORY|O_NOFOLLOW` 打开日志目录，再以 `dir_fd` 和 `O_NOFOLLOW` 打开历史明文文件名与新哈希文件名，拒绝符号链接、硬链接、非普通文件；单文件最大读取 16 MiB。
- `server.py` 恢复上传游标时遇到不安全/不可读日志，返回 HTTP 500，拒绝继续接收可能重复的事件；`webui.py` 遇到不安全日志返回 HTTP 503，不显示目标文件内容。不存在的日志仍按空日志处理。
- 本轮 **没有**修改历史文件内容、权限、目录结构或生产数据。旧日志过大、权限不足或不支持 POSIX no-follow 时会拒绝读取，需人工离线检查，不能自动删除或放宽权限。
- 初次 CI 因新测试 JSONL 换行转义错误失败 1/153，修复提交 `b1587ca5` 后 Push [40696479322](https://github.com/linyubin/InkTime/actions/runs/40696479322) 与 PR [40696487199](https://github.com/linyubin/InkTime/actions/runs/40696487199) 均 SUCCESS：153 Python、4 Node。未验证真实 NAS、HTTPS 浏览器、Docker/systemd、ESP32 和人工签核。
- 未解决：祖先目录符号链接、并发轮转、跨进程上传游标、`fsync` 持久化及 HTTP bearer 密钥暴露。仅允许在开发分支逆序 `git revert b1587ca5`、`git revert e1cea979` 并重跑 CI；回退会重新允许历史日志符号链接跟随，**不是生产安全回滚**。R1 Gate 仍 OPEN。

## 2026-10-29 — 跨进程日志锁与轮转游标恢复（仅开发分支）

- 服务端为每个哈希日志文件创建长期保留的 `device-<sha256(key)>.log.lock`，以 POSIX `flock` 排他锁保护「磁盘游标恢复 → 过滤重复 `ls` → 追加/轮转」完整事务。锁文件以 `0600` 创建；符号链接、硬链接、特殊文件和被替换的锁 inode 都拒绝使用。
- **禁止定时清理或手动删除仍在使用的 `.lock` 文件。** 删除后重新创建会产生不同 inode，可能导致两个进程同时持有不同锁。所有并行运行的服务器 worker 必须统一升级到这一版本；混用旧版写入者不安全。
- 服务器每次请求都在锁内从磁盘读取最大 `ls`，不再信任进程内缓存；读取旧/新文件的 `.log` 和 `.log.1`，避免轮转后、首次写入前崩溃造成游标倒退。旧日志保持只读；设备 API 格式不变。
- 隔离验证命令：`python -m unittest discover -s tests -p 'test_device_log_storage.py' -v`。CI [Push 40726336429](https://github.com/linyubin/InkTime/actions/runs/40726336429) 与 [PR 40726349050](https://github.com/linyubin/InkTime/actions/runs/40726349050) 均通过：156 Python + 4 Node（全量），含两个独立进程的锁序列化测试。
- **未验证**：NAS/NFS/SMB 跨主机锁语义、外部日志轮转、上层目录符号链接、锁等待上限、磁盘满/掉电后的持久性、真实 ESP32 上传、HTTPS 浏览器、Docker/systemd 和人工签核。当前写入尚未 `fsync`，HTTP 200 不应被解释为掉电后必然持久；共享密钥的多设备 `ls` 冲突仍存在。
- 回滚仅限独立开发分支撤销 `834bf426` 并重跑 CI；这会恢复多进程重复写入/轮转竞争，**不能作为生产安全回滚**。不要在生产 NAS 上删除锁文件或操作历史日志。R1 Gate 仍为 OPEN。


## 2026-10-29 — 日志 fsync 与重复上传确认门禁（开发分支）

- 服务器在每次新设备日志追加后，先刷新 Python 缓冲、`fsync` 日志文件，再 `fsync` 日志目录，全部成功后才返回 HTTP 200。文件或目录同步失败返回 HTTP 500，不推进服务端诊断游标。
- 上一次 `fsync` 失败后，数据可能已写入页缓存但尚未持久化；设备重试时服务端可能判定为重复。为避免未经同步便确认，**即使没有新事件**，也会在同一个跨进程锁内对现有哈希日志 `.log.1`、`.log` 和目录执行 `fsync`。不存在的日志文件不会被创建。
- 验证：`python -m unittest discover -s tests -p 'test_device_log_storage.py' -v`；Push [40754304629](https://github.com/linyubin/InkTime/actions/runs/40754304629) / PR [40754314641](https://github.com/linyubin/InkTime/actions/runs/40754314641) **SUCCESS**，全量 160 Python + 4 Node。包括实际 Flask 上传处理函数的同步失败、重试、重复确认故障注入。
- **仍未验证**：真实 NAS/NFS/SMB 的持久化与跨主机锁、掉电恢复、部分 JSONL 写入、上层目录符号链接、外部日志轮转、设备身份、HTTPS 浏览器、Docker/systemd、ESP32 真机和人工签核。即使系统调用成功，也不能在未验证存储硬件前宣称端到端掉电不丢数据。
- 开发分支回滚：`git revert 312928f0` 并重新运行 CI；这会恢复 HTTP 200 先于数据持久化的风险，不可作为未经审批的生产回滚。R1 Gate 仍 **OPEN**。

## 2026-10-29 — 活动日志不完整尾部恢复（仅开发分支）

- 服务端在已有 `flock` 内、恢复 `ls` 之前检查当前哈希命名的 `.log`。如果文件最后不是换行符，则最后一段视为**未确认的部分 JSONL 写入**；只截断该段，保留此前全部完整行。截断后必须完成文件和目录 `fsync` 才能继续接受设备重试。
- 为避免静默丢弃大量数据，单段不完整尾部超过 64 KiB 时返回 HTTP 500 并要求人工检查。符号链接、硬链接、文件被替换、同步失败也全部拒绝处理；服务端诊断游标不推进。
- 该恢复会**自动丢弃未以换行结束的字节**，不保留单独的法证副本。需要保留故障现场时，应先在隔离环境制作快照；不要直接对真实 NAS 日志执行试验。旧版明文日志和 `.log.1` 轮转文件不会被自动截断。
- 实现 `53dabb5a`，测试修复 `26bd8fad`。初次 CI 因测试换行转义问题失败 4/165，修复后 [Push 40779798245](https://github.com/linyubin/InkTime/actions/runs/40779798245) **SUCCESS**（165 Python + 4 Node）；[PR 40779807991](https://github.com/linyubin/InkTime/actions/runs/40779807991) 初次记录时仍在运行。均为本地 POSIX 文件/Flask 故障注入，不是真实 NAS 掉电或设备验收。
- 未解决：已带换行但内容损坏的 JSONL、历史/轮转文件部分写入、NAS/NFS/SMB 持久化、跨主机锁、外部轮转、HTTPS 浏览器、Docker/systemd、ESP32 和人工签核。R1 Gate 仍 OPEN。
- 开发分支回滚：先撤销本轮文档提交，再撤销 `26bd8fad` 和 `53dabb5a`，重跑 CI。此回滚会恢复部分写入导致的日志拼接风险，不可直接作为生产回滚。
