#!/usr/bin/env bash
set -euo pipefail
cd -- "$(dirname -- "${BASH_SOURCE[0]}")"
if [[ "${EUID}" -ne 0 ]]; then
  echo '请使用 sudo bash install.sh' >&2
  exit 1
fi
missing=()
command -v python3 >/dev/null || missing+=(python3)
command -v ss >/dev/null || missing+=(iproute2)
if ((${#missing[@]})); then
  apt-get update
  apt-get install -y "${missing[@]}"
fi
python3 -c 'import sys; assert sys.version_info >= (3, 10), "需要 Python 3.10+（Ubuntu 22.04+）"'
config=config.json
if [[ -f /etc/service-portal/config.json ]]; then
  config=/etc/service-portal/config.json
fi
# Validate before replacing the installed application.
python3 -c 'import sys; from portal import load_config; load_config(sys.argv[1])' "$config"
if ! getent passwd service-portal >/dev/null; then
  useradd --system --user-group --home-dir /nonexistent --no-create-home --shell /usr/sbin/nologin service-portal
fi
install -d -m 755 /opt/service-portal /etc/service-portal
install -m 644 portal.py index.html /opt/service-portal/
if [[ ! -f /etc/service-portal/config.json ]]; then
  install -m 644 config.json /etc/service-portal/config.json
fi
install -m 644 service-portal.service /etc/systemd/system/service-portal.service
systemctl daemon-reload
systemctl enable service-portal
systemctl restart service-portal
sleep 2
if ! systemctl is-active --quiet service-portal; then
  journalctl -u service-portal -n 40 --no-pager
  exit 1
fi
echo '安装完成。默认访问 http://服务器IP；若修改了配置，请使用对应端口。'
echo '配置：/etc/service-portal/config.json；日志：journalctl -u service-portal -f'
