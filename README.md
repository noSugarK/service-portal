# Ubuntu 本机服务导航

自动读取 Ubuntu 的 TCP 监听端口，探测 HTTP/HTTPS，展示网页标题、端口、响应状态与访问链接。Python 标准库实现，无 pip 依赖、无需 Docker 或数据库。适用于 Ubuntu 22.04 / 24.04，Python 3.10+，systemd。

页面支持按名称、端口搜索与 HTTP/HTTPS 筛选，展示服务总数、HTTPS 数量及响应状态，适配手机访问。

## 1. 安装

把整个目录上传到 Ubuntu，例如 `~/service-portal`。进入该目录执行：

```bash
cd ~/service-portal
sudo ss -ltn 'sport = :80'
python3 -m unittest -v
sudo bash install.sh
```

安装脚本仅在缺少 python3 或 ss 时通过 apt 补装依赖，并在覆盖程序前校验配置；随后创建无登录权限的 `service-portal` 系统账户，自动启动服务并检查运行状态。应用安装到 `/opt/service-portal`，运行配置为 `/etc/service-portal/config.json`，服务为 `/etc/systemd/system/service-portal.service`。补装依赖时需要主机可以访问 Ubuntu 软件源。

默认直接监听 IPv4 `0.0.0.0:80`。浏览器打开 `http://服务器IP`。程序以普通系统账户运行，systemd 仅授予绑定低端口的 `CAP_NET_BIND_SERVICE` 权限。首次扫描需要少量时间；每次扫描完成后等待 60 秒再次扫描。

如果 80 已被占用，先按下面说明修改配置再安装，或修改安装后的运行配置并重启。安装失败时查看日志，不会自动停止已有服务。

若启用了 UFW，按实际内网网段放行入口，例如：

```bash
sudo ufw allow from 192.168.1.0/24 to any port 80 proto tcp
```

请替换为自己的内网网段。还需按需放行目标服务端口，以及云主机安全组规则；安装脚本不修改防火墙。

## 2. 配置

首次安装前可编辑目录中的 `config.json`。安装后始终编辑 `/etc/service-portal/config.json`；重复安装保留该配置。完整示例：

```json
{
  "bind": "0.0.0.0",
  "port": 80,
  "scan_interval": 60,
  "probe_timeout": 1.5,
  "workers": 16,
  "include_loopback": false,
  "exclude_ports": [22, 5432, 6379],
  "docker_enabled": false,
  "services": {
    "3000": {"name": "监控面板", "path": "/"},
    "8080": {"name": "管理后台", "path": "/admin/"}
  }
}
```

| 字段 | 说明 |
| --- | --- |
| bind | 入口监听 IP；默认全部 IPv4 地址，可用 `::` 监听 IPv6，IPv4 双栈行为取决于系统配置 |
| port | 导航入口端口，自身端口自动排除 |
| scan_interval | 每轮扫描完成后的等待秒数，10–86400 |
| probe_timeout | 单次网络操作超时秒数，0.2–10；并非整轮扫描的总超时 |
| workers | 并发探测数，1–64 |
| include_loopback | 是否展示仅监听回环地址的服务；默认不展示，开启后展示说明且不提供远程链接 |
| exclude_ports | 不探测、不展示的端口列表 |
| services | 按端口设置名称和入口路径；仍需实际监听且能响应 HTTP/HTTPS，不会创建虚假的在线卡片 |
| docker_enabled | 开启 Docker 运维 API，默认 false；开启时必须配置访问令牌与 socket 权限，见下文 |

修改后执行：

```bash
sudo systemctl restart service-portal
sudo journalctl -u service-portal -n 50 --no-pager
```

网页可见时每 10 秒拉取缓存；“刷新列表”立即拉取最新缓存，不强制重新扫描，避免访问者触发大量探测。扫描或连接失败会保留最后成功结果，页面显示错误和上次成功时间。HTTPS 数量仅表示协议类型；响应状态与客户端实际能否访问需分别判断。

## 3. 80 已有 Nginx 的情况（可选）

将运行配置修改为 `"bind": "127.0.0.1"`、`"port": 9080`，重启服务。参考 `nginx.conf.example` 将导航页面挂到独立域名，并将域名解析到服务器 IP；替换示例中的 `portal.example.com`。

```bash
sudo cp nginx.conf.example /etc/nginx/sites-available/service-portal
sudo editor /etc/nginx/sites-available/service-portal
sudo ln -s /etc/nginx/sites-available/service-portal /etc/nginx/sites-enabled/service-portal
sudo nginx -t
sudo systemctl reload nginx
```

如果希望通过 IP 直接访问，把代理配置合并到现有处理该 IP 请求的 server 中；不要盲目添加重复的默认站点。示例只代理导航页面，目标服务仍直接通过各自端口访问。公网使用时，应由现有 Nginx 或网关配置 HTTPS、认证和访问控制；导航页默认没有登录功能。

## 4. 发现与访问的边界

- 只查询宿主机当前网络命名空间中的监听 TCP socket，不扫描其他机器，不读取进程信息，不需要 root 探测权限。
- 每个监听地址先尝试 HTTPS，再尝试 HTTP，避免把 HTTPS 端口对明文请求返回的 400 误认成 HTTP 服务；根路径或自定义路径返回有效 HTTP 状态即视为网页服务，包括 301/302、401/403、404/500。不跟随重定向，标题缺失时使用端口名。HTTP 检测会发送 GET，请把不应探测的端口加入排除列表。
- 本地 HTTPS 探测接受自签名证书，浏览器访问时仍执行正常证书校验；标题只读取最多 32 KiB 的首批数据，按 UTF-8 解析，不执行 JavaScript。
- 依赖指定域名虚拟主机、特殊 Host 头、双向 TLS、特殊根路径或非 UTF-8 标题的服务可能无法识别或无法提取标题。按需设置路径、别名，域名专用入口需另行扩展。
- 链接使用浏览器当前地址中的主机名与发现端口。仅绑定某张网卡的服务，使用另一张网卡地址访问导航页时可能不可达；本地检测成功也不保证防火墙、NAT、云安全组允许客户端访问。
- Docker 使用 userland-proxy 发布到宿主机的端口通常能被 ss 发现；纯 iptables/nftables DNAT、无宿主监听 socket 的发布端口不会被发现。容器内部未发布的服务、其他网络命名空间也不会被发现。
- 此版本是导航入口，不代理目标服务；只开放 80 不能让用户访问所有其他端口。默认用于可信内网，公网访问还需要认证与访问控制。
- `/healthz` 返回进程 HTTP 存活状态；扫描状态查看 `/api/services` 的 `error`、`updated_at`，或查看日志。

## 5. 运维、升级与卸载

```bash
sudo systemctl start service-portal
sudo systemctl stop service-portal
sudo systemctl restart service-portal
sudo journalctl -u service-portal -f
```

升级：上传新代码，重新执行 `sudo bash install.sh`；会覆盖程序和 systemd 单元，保留 `/etc/service-portal/config.json`。重新安装前按需备份自定义 systemd 配置，建议通过 `systemctl edit service-portal` 写入 override。

卸载（保留配置和系统账户）：

```bash
sudo systemctl disable --now service-portal
sudo rm /etc/systemd/system/service-portal.service
sudo systemctl daemon-reload
sudo rm /opt/service-portal/portal.py /opt/service-portal/index.html
```

## 6. Docker 基础运维（可选）

Docker 运维是独立的 `/docker` 页面，直接输入服务器地址加 `/docker` 访问；服务导航页不提供跳转链接。支持全部容器列表、搜索、最近日志、启动、停止和重启。列表手动刷新；停止与重启前需要确认。暂停中的容器仅提供查看与日志，暂不支持恢复、删除、终端、镜像或 Compose 管理。

默认关闭 Docker API，此时安装脚本不授予 Docker 权限。配置 `docker_enabled: true` 后重新执行安装脚本，会自动为服务配置 `SupplementaryGroups=docker`，无需手工编辑 systemd。启用需要宿主机已经安装并运行 Docker Engine，socket 为 `/var/run/docker.sock`；安装前会检查 docker 用户组和 socket 是否存在，缺失时停止安装并提示。暂不支持 rootless Docker 和远程 Docker，无需安装额外 Python 包或 Docker CLI。

Docker socket 控制权限通常等同于主机 root 权限。所有 Docker API 都需要令牌；容器名称、镜像、日志不会通过匿名服务发现 API 返回。页面允许通过内网 IPv4 地址（10.0.0.0/8、172.16.0.0/12、192.168.0.0/16）或 localhost / 回环地址使用 HTTP 令牌登录，例如 `http://10.22.29.66/docker`。HTTP 不加密令牌，仅在可信内网使用；公网 IP、域名（包括内网域名）仍需 HTTPS。该页面检查不替代防火墙或代理的访问限制，也不会为后端 API 自动限制来源 IP。普通服务导航仍可使用 HTTP。

1. 已安装时编辑 `/etc/service-portal/config.json`，增加或修改 `"docker_enabled": true`；首次安装则修改项目中的 `config.json`。旧配置未包含该字段时仍默认关闭。
2. 创建仅 root 可读的令牌文件（文件已存在时会报错，不会覆盖原令牌）：

   ```bash
   sudo install -d -m 755 /etc/service-portal
   sudo python3 -c 'import os,secrets; fd=os.open("/etc/service-portal/docker.env", os.O_WRONLY|os.O_CREAT|os.O_EXCL, 0o600); os.write(fd, ("SERVICE_PORTAL_DOCKER_TOKEN="+secrets.token_hex(32)+"\n").encode()); os.close(fd)'
   ```

3. 上传新版代码并执行安装。脚本会读取运行配置，自动配置 Docker 权限并重启服务：

   ```bash
   sudo bash install.sh
   sudo systemctl show service-portal -p SupplementaryGroups
   sudo journalctl -u service-portal -n 50 --no-pager
   ```

   `SupplementaryGroups` 应包含 `docker`。权限由 `/etc/systemd/system/service-portal.service.d/90-service-portal-docker.conf` 管理；重复安装保留现有运行配置和令牌。

4. 在服务器终端执行 `sudo cat /etc/service-portal/docker.env`，将等号后的令牌输入 Docker 页面。不要将令牌放入 URL 或分享截图。令牌仅保留在当前页面内存中，刷新页面或断开连接后需要重新输入。使用反向代理时需保留原 Host（已有 Nginx 示例已配置），并透传 Authorization 请求头。

若通过 SSH 本机转发使用，可执行 `ssh -L 9080:127.0.0.1:80 用户@服务器`，然后在本机打开 `http://localhost:9080/docker`。若导航入口端口不是 80，请替换转发目标端口。

日志最多读取最近 200 行和 256 KiB，不实时追踪；日志驱动不支持读取时会报告 Docker 错误。接口只接受完整容器 ID 与固定的启动、停止、重启、日志操作，不执行 shell 命令。请求超时后容器操作仍可能完成，应先刷新确认状态再重试。

关闭入口并撤销自动配置的 socket 权限：设置 `"docker_enabled": false`，重新执行 `sudo bash install.sh`。安装脚本只删除自己管理的权限文件，保留其他自定义 systemd 配置；如果以前手工添加过 `docker.conf` 或其他包含 `SupplementaryGroups=docker` 的 override，还需手工移除这些授权，再执行 daemon-reload 和 restart。令牌轮换：修改 `/etc/service-portal/docker.env` 后重启服务，原令牌立即失效；不要更改其 600 权限。

## 7. 验证与排障

```bash
python3 -m unittest -v
node test_ui.js  # 可选：前端访问地址策略检查，仅开发验证需要 Node.js
sudo ss -H -ltn
curl http://127.0.0.1/api/services
sudo journalctl -u service-portal -n 100 --no-pager
```

测试覆盖监听地址解析、过滤与去重、本地真实 HTTP 标题提取、认证状态、重定向不跟随、HTTPS 回退逻辑和配置路径校验。HTTPS 回退使用 mock，不替代实际 Ubuntu 上的 TLS 和 systemd 验证。

Docker 测试覆盖默认禁用、HTTP 令牌认证、跨站请求拦截、操作白名单、列表解析、TTY/非 TTY 日志解析与大小限制；使用模拟 Docker 响应，不会操作真实容器，也不能替代 Ubuntu 上的 Docker socket 权限与真实启停验证。

Linux / WSL 测试还会在临时目录模拟安装，验证自动配置 Docker 权限、保留运行配置、关闭后撤销自动授权、保留管理员 override，以及用户组或 socket 缺失时提前退出；不会修改真实系统服务。Windows 运行时跳过该安装测试。

常见情况：端口冲突检查 `ss -ltn 'sport = :80'`；空列表检查服务是否只绑定 `127.0.0.1`、是否被排除、是否为 Docker DNAT；点击失败检查目标端口防火墙和网卡绑定地址。
