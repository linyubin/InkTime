# R1/PR-02 — endpoint.json 安全持久化（开发分支）

## 实现范围

设备自动注册 \`/api/device_register/<key>\` 与管理员手动设置 \`/api/device/set_endpoint\` 共用 \`device_endpoint_storage.py\`。旧版 \`Path.write_text\` 直接覆盖 \`logs/device/endpoint.json\`，断电/写入中断可能产生半截 JSON，也可能跟随符号链接。

- 逐级以 \`O_DIRECTORY|O_NOFOLLOW\` 打开目录；拒绝符号链接祖先与 \`..\`，沿用日志目录的安全目录描述符机制。
- 读取仅允许不超过 4096 字节的普通单链接文件；检查 inode/大小/mtime 及目录身份，随后严格校验 JSON 和 RFC1918 IPv4/端口。损坏或不安全文件视为设备离线，不发起 LAN 回调。
- 写入先校验记录，创建随机命名的 \`0600\` 临时文件，循环处理短写，文件 \`fsync\` 成功后使用同一目录 fd 原子替换 \`endpoint.json\`，然后执行目录 \`fsync\` 并再次核对目录身份。临时文件在正常失败路径清理；现有不安全目标拒绝替换。
- 设备注册协议与 HTTP 返回格式不变。任一持久化错误都不得返回成功 ACK。**如果目录 fsync 在 rename 之后失败，记录可能已经可见但请求返回 HTTP 500；设备重试是允许的。**
- 新目录请求 \`0700\`，不自动更改已有目录权限。旧记录允许正常只读兼容，下一次成功注册将以 \`0600\` 文件原子替换。

## 验证

\`\`\`bash
python -m unittest discover -s tests -p 'test_*.py' -v
\`\`\`

代码提交 \`88cea02d\`： [Push CI 40858058716](https://github.com/linyubin/InkTime/actions/runs/40858058716) / [PR CI 40858071214](https://github.com/linyubin/InkTime/actions/runs/40858071214) **SUCCESS**（185 Python + 4 Node、依赖安装、编译）。七项独立文件系统故障注入覆盖权限、符号/硬链接、祖先路径、无效/超大 JSON、短写、文件/目录 fsync；后续增加 Flask HTTP 500 回归测试。**仅为 Linux CI 证据。**

## 限制、风险与回滚

- 没有真正的设备身份认证：持有下载密钥的其他设备仍可注册局域网地址；跨进程并发注册是 last-writer-wins。混合版本服务进程不能保证安全。
- 对不遵守本协议的进程直接原地修改 \`endpoint.json\`，无法提供完整并发隔离。最后一次目录身份检查之后仍有外部路径替换竞争窗口。
- NAS/NFS/SMB 的目录 fsync、rename、权限和断连语义，HTTPS 浏览器/CSRF、Docker/systemd、ESP32 编译/真机及人工签核均**未验证**；不能把 CI 当作生产验收。R1 Gate OPEN，R2/R3 未通过。
- 仅可在独立分支撤销本轮文档/测试提交，再撤销 \`88cea02d\` 并重跑 CI。该回滚恢复非原子写入与符号链接风险，**不得作为生产回滚方案**。不得修改真实 NAS 数据、合并 PR、部署或刷写 ESP32。
