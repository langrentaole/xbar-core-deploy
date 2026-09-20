#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
ENV_FILE=${XBAR_CORE_ENV_FILE:-"$ROOT_DIR/.env"}
export XBAR_CORE_ENV_FILE=$ENV_FILE

compose() {
  docker compose --project-directory "$ROOT_DIR" --env-file "$ENV_FILE" -f "$ROOT_DIR/docker-compose.yml" "$@"
}

random_hex() {
  openssl rand -hex "$1"
}

replace_value() {
  key=$1
  value=$2
  temporary="$ENV_FILE.tmp.$$"
  awk -v key="$key" -v value="$value" 'BEGIN { FS = "=" } $1 == key { print key "=" value; next } { print }' "$ENV_FILE" >"$temporary"
  mv "$temporary" "$ENV_FILE"
}

read_value() {
  sed -n "s/^$1=//p" "$ENV_FILE" | tail -n 1
}

template_value() {
  sed -n "s/^$1=//p" "$ROOT_DIR/.env.example" | tail -n 1
}

sync_release() {
  image=$(template_value XBAR_CORE_IMAGE)
  [ -n "$image" ] || {
    echo "部署仓库缺少 XBAR_CORE_IMAGE 发布版本" >&2
    exit 1
  }
  if [ "$(read_value XBAR_CORE_IMAGE)" != "$image" ]; then
    replace_value XBAR_CORE_IMAGE "$image"
    echo "已同步 Core 镜像版本：$image"
  fi
}

show_station_id() {
  station_id=$(compose exec -T -e "MYSQL_PWD=$(read_value MYSQL_PASSWORD)" db mysql -N -B -uxbar xbar -e "SELECT COALESCE(JSON_UNQUOTE(JSON_EXTRACT(certificate_json,'$.claims.stationId')),'') FROM local_station_license WHERE id=1 AND status='active' LIMIT 1" | tail -n 1)
  if [ -z "$station_id" ] || [ "$station_id" = "NULL" ]; then
    echo "当前 Core 尚未完成有效授权，无法取得 stationId" >&2
    exit 1
  fi
  printf '%s\n' "$station_id"
}

initialize() {
  if [ -f "$ENV_FILE" ]; then
    echo "环境文件已存在，未覆盖：$ENV_FILE"
    return
  fi
  command -v openssl >/dev/null 2>&1 || {
    echo "缺少 openssl，无法生成安全密钥" >&2
    exit 1
  }
  cp "$ROOT_DIR/.env.example" "$ENV_FILE"
  replace_value MYSQL_PASSWORD "$(random_hex 24)"
  replace_value MYSQL_ROOT_PASSWORD "$(random_hex 24)"
  replace_value COOKIE_KEY "$(random_hex 32)"
  replace_value DATA_KEY "$(random_hex 16)"
  replace_value VERIFICATION_KEY "$(random_hex 32)"
  replace_value EDGE_INTERNAL_KEY "$(random_hex 32)"
  replace_value VIBE_EDGE_SHARED_SECRET "$(random_hex 32)"
  replace_value PAYMENT_MERCHANT_KEY "$(random_hex 32)"
  replace_value ADMIN_PASSWORD "$(random_hex 12)"
  chmod 600 "$ENV_FILE"
  echo "已生成：$ENV_FILE"
  echo "请修改域名、邮箱和用户端来源，然后把 VIBE_EDGE_SHARED_SECRET 原样复制到 xbar-vibe-deploy/.env。"
}

validate() {
  [ -f "$ENV_FILE" ] || {
    echo "缺少环境文件，请先执行：./deploy.sh init" >&2
    exit 1
  }
  if grep -Eq '^[A-Z0-9_]+=(replace-with-|.*\.example\.com($|/))' "$ENV_FILE"; then
    echo "环境文件仍有占位值，请先完成配置：$ENV_FILE" >&2
    exit 1
  fi
  for key in XBAR_CORE_IMAGE CORE_DOMAIN ACME_EMAIL USER_UPSTREAM VIBE_GATEWAY_URL PAYMENT_MERCHANT_ID PAYMENT_MERCHANT_KEY PAYMENT_BASE_URL MYSQL_PASSWORD MYSQL_ROOT_PASSWORD COOKIE_KEY DATA_KEY VERIFICATION_KEY EDGE_INTERNAL_KEY VIBE_EDGE_SHARED_SECRET ADMIN_USERNAME ADMIN_EMAIL ADMIN_PASSWORD TIMEZONE; do
    [ -n "$(read_value "$key")" ] || {
      echo "缺少必填配置：$key" >&2
      exit 1
    }
  done
  case "$(read_value USER_UPSTREAM)" in http://*|https://*) ;; *) echo "USER_UPSTREAM 必须是 HTTP(S) 地址" >&2; exit 1 ;; esac
  case "$(read_value VIBE_GATEWAY_URL)" in https://*) ;; *) echo "VIBE_GATEWAY_URL 必须是 HTTPS 地址" >&2; exit 1 ;; esac
  case "$(read_value PAYMENT_BASE_URL)" in https://*) ;; *) echo "PAYMENT_BASE_URL 必须是 HTTPS 地址" >&2; exit 1 ;; esac
  [ "$(printf %s "$(read_value DATA_KEY)" | wc -c | tr -d ' ')" -eq 32 ] || {
    echo "DATA_KEY 必须恰好为 32 字节" >&2
    exit 1
  }
  for key in COOKIE_KEY VERIFICATION_KEY EDGE_INTERNAL_KEY VIBE_EDGE_SHARED_SECRET PAYMENT_MERCHANT_KEY; do
    [ "$(printf %s "$(read_value "$key")" | wc -c | tr -d ' ')" -ge 32 ] || {
      echo "$key 至少需要 32 字节" >&2
      exit 1
    }
  done
  compose config -q
}

require_docker() {
  command -v docker >/dev/null 2>&1 || {
    echo "未安装 Docker" >&2
    exit 1
  }
  docker compose version >/dev/null 2>&1 || {
    echo "未安装 Docker Compose v2" >&2
    exit 1
  }
}

action=${1:-up}
case "$action" in
  init)
    initialize
    ;;
  config)
    require_docker
    validate
    echo "xbar-core 配置有效"
    ;;
  up|install)
    require_docker
    if [ ! -f "$ENV_FILE" ]; then
      initialize
      exit 2
    fi
    validate
    compose pull
    compose up -d --wait --remove-orphans
    compose ps
    ;;
  update)
    require_docker
    sync_release
    validate
    compose pull core-api core-edge
    compose up -d --wait --remove-orphans
    compose ps
    ;;
  station-id)
    require_docker
    validate
    show_station_id
    ;;
  status)
    require_docker
    validate
    compose ps
    ;;
  logs)
    require_docker
    validate
    compose logs -f --tail=200
    ;;
  down)
    require_docker
    validate
    compose down
    ;;
  *)
    echo "用法：$0 {init|config|up|update|station-id|status|logs|down}" >&2
    exit 64
    ;;
esac
