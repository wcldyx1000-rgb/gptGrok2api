#!/bin/sh
# Ported from yukkcat/chatgpt2api (AGPL-3.0-only), simplified for gptGrok2api.
#
# 默认（未设置 GPTGROK2API_MANAGED_RUNTIME=1）：什么都不做，直接 exec 原命令，
# 行为与改动前完全一致（应用文件来自镜像，升级方式是拉取新镜像）。
#
# 受管运行目录模式（GPTGROK2API_MANAGED_RUNTIME=1，且 /app 挂载了持久卷）：
# 首次启动或镜像版本变化时，用镜像内种子目录 /opt/gptgrok2api 覆盖 /app 下的应用文件
# （保留 data、logs、config.json、.venv 与更新备份），随后同步依赖。这样管理台“一键更新”
# 写入 /app 的新版本在容器重启后依然生效。
set -eu

if [ "${GPTGROK2API_MANAGED_RUNTIME:-0}" != "1" ]; then
  exec "$@"
fi

seed_root=/opt/gptgrok2api
runtime_root=/app
marker_name=.gptgrok2api-image-version
seed_version="$(tr -d '\r\n' < "${seed_root}/VERSION")"
installed_image_version=""

if [ -f "${runtime_root}/${marker_name}" ]; then
  installed_image_version="$(tr -d '\r\n' < "${runtime_root}/${marker_name}")"
fi

# 仅当镜像版本变化（或首次）才重置应用文件；在线更新后 VERSION 不同于 marker 也不会被覆盖。
if [ ! -f "${runtime_root}/VERSION" ] || [ "${installed_image_version}" != "${seed_version}" ]; then
  mkdir -p "${runtime_root}"
  find "${runtime_root}" -mindepth 1 -maxdepth 1 \
    ! -name data \
    ! -name logs \
    ! -name config.json \
    ! -name .venv \
    ! -name "${marker_name}" \
    -exec rm -rf -- {} +

  for source in "${seed_root}"/* "${seed_root}"/.[!.]* "${seed_root}"/..?*; do
    [ -e "${source}" ] || continue
    [ "$(basename "${source}")" = ".venv" ] && continue
    cp -a "${source}" "${runtime_root}/"
  done

  marker_tmp="${runtime_root}/${marker_name}.tmp"
  printf '%s\n' "${seed_version}" > "${marker_tmp}"
  mv "${marker_tmp}" "${runtime_root}/${marker_name}"
fi

cd "${runtime_root}"
uv sync --frozen --no-dev --no-install-project
exec "$@"
