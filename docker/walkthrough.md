# InkTime Unraid Docker 部署指南与总结

所有与容器化部署相关的文件已成功生成并存放在项目根目录下的 [docker](file:///e:/InkTime/docker/) 目录中。

## 生成文件总览

| 文件名 | 用途说明 |
| :--- | :--- |
| [Dockerfile](file:///e:/InkTime/docker/Dockerfile) | 定义整个轻量级 Python 系统的基础镜像，并打包必备环境变量。 |
| [entrypoint.sh](file:///e:/InkTime/docker/entrypoint.sh) | 容器专属启动脚本。负责检测 `config.py`，配置映射环境与初始化每天早上的 cron。 |
| [supervisord.conf](file:///e:/InkTime/docker/supervisord.conf) | 进程守护配置文件，同步在后台监控运行 `webui.py` 与前台守卫 `cron`。 |
| [inktime-unraid-template.xml](file:///e:/InkTime/docker/inktime-unraid-template.xml) | Unraid 专用的一键导入 XML 表单配置，避免手动逐参数添加容器。 |

## 在 Unraid 上部署的使用步骤

> [!NOTE]
> 部署前，您需要先将最新整合了 `docker` 目录的代码推送到您的 NAS，或者 clone 到 NAS的任意位置准备构建镜像。

### 步骤一：在宿主机上构建镜像
用 SSH 登录您的 Unraid（或通过内置 Terminal），导航到代码所在的目录。执行以下命令构建名为 `inktime` 的 Docker 镜像：
```bash
docker build -f docker/Dockerfile -t inktime:latest .
```

### 步骤二：向 Unraid 添加应用模板

您可以任选以下两种方式来安装这个 Docker：

**方法 A（推荐）：导入模板**
1. 找到您 Unraid U 盘配置目录（通常是 `\\<Tower_IP>\flash\config\plugins\dockerMan\templates-user`）
2. 将刚才生成的 `docker/inktime-unraid-template.xml` 扔进该文件夹。
3. 在 Unraid 面板的 **Docker** 选项卡下，点击底部的 **Add Container**。
4. 从最顶部的 `Template` 下拉框中，选择刚刚放入的 `InkTime` 模板。相关参数将全部自动为您填好。

**方法 B：手动填写参数（Add Container 时）**
- **Repository**: `inktime:latest`
- **Network Type**: `host`
- 追加 **Path mappings**:
  - `Container Path`: `/config`  -> `Host Path`: `/mnt/user/appdata/inktime`（可读写）
  - `Container Path`: `/photos`  -> `Host Path`: 您存放照片的绝对路径如 `/mnt/user/Photos`（只读 Read Only!!）
  - `Container Path`: `/var/run/dbus` -> `Host Path`: `/var/run/dbus` （用以穿透底层让蓝牙生效）
- 追加 **Variable**:
  - `Key`: `CRON_SCHEDULE`  -> `Value`: `30 7 * * *` （若无需求可全留空以用默认配置）

### 步骤三：初始化与配置修改
> [!IMPORTANT]
> 首次启动这个新建的容器后，检查您的 `appdata/inktime` 文件夹，必定会看到系统自动拉取生成的 `config.py`。
> 请用任意文本编辑器打开这个位于宿主机上的 `/mnt/user/appdata/inktime/config.py`，**必须将其中 `IMAGE_DIR` 参数修改为 `"/photos"`**！
> 其他 API 的参数如果之前在 Windows 填好了，不用动。保存后可以重启容器以正式生效。

### 日志排查
如果您发现定时任务没触发，或是遇到蓝牙无法连接的问题：
您可以进入 Unraid 宿主机的 `/mnt/user/appdata/inktime/logs/` 目录，这里会实时生成和保留 `cron_render.log` 与 `webui.log`。您可以清晰看到脚本每日推送时的终端真实控制台输出，无论是超时或错误（譬如 124 中断）。

Enjoy! 你的电子水墨相框已经完美封装！
