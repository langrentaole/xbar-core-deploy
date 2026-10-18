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

guard_billing_upgrade() {
  command -v python3 >/dev/null 2>&1 || {
    echo "缺少 python3，无法安全检查账务数据库版本" >&2
    exit 1
  }
  # Resolve Compose's actual project/volume names, including operator overrides.
  resolved=$(compose config --format json | python3 -c '
import json, sys
config = json.load(sys.stdin)
for value in (config["name"], config["volumes"]["mysql-data"]["name"]):
    if not value or any(c.isspace() for c in value):
        raise SystemExit("Invalid Compose project/volume name")
    print(value)
')
  project=$(printf '%s\n' "$resolved" | sed -n '1p')
  db_volume=$(printf '%s\n' "$resolved" | sed -n '2p')
  db_container=$(compose ps --all -q db)
  if [ -z "$db_container" ]; then
    containers=$(compose ps --all -q)
    volumes=$(docker volume ls --format '{{.Name}}')
    if [ -n "$containers" ] || printf '%s\n' "$volumes" | grep -Fxq "$db_volume"; then
      echo "发现已有部署或数据库卷，但没有可检查的 db 容器。请先仅恢复 db/cache 并检查数据库版本；不要启动 API/Edge。" >&2
      exit 1
    fi
    return
  fi
  if ! schema=$(compose exec -T db sh -c 'export MYSQL_PWD="$MYSQL_ROOT_PASSWORD"; exec mysql -uroot -N -B xbar -e "SELECT MAX(version_id) FROM goose_db_version WHERE is_applied=1"'); then
    echo "无法读取现有数据库版本；请仅恢复 db/cache 后重试，不要启动 API/Edge。" >&2
    exit 1
  fi
  case "$schema" in
    127)
      echo "schema 127：进入协调升级，将停用 Edge/API、备份数据库并升级至发布镜像支持的 schema 129/130。"
      exec python3 "$ROOT_DIR/billing_upgrade.py" \
        --compose-file "$ROOT_DIR/docker-compose.yml" --env-file "$ENV_FILE" \
        --project "$project" --image "$(template_value XBAR_CORE_IMAGE)" \
        --state-dir "${XBAR_CORE_UPGRADE_STATE_DIR:-/var/lib/xbar-core/billing-upgrade-129}" --apply
      ;;
    129|130) return ;;
    128)
      echo "拒绝自动部署 schema 128。请保持旧 API/Edge 停止并检查原始升级记录；只有 billing_upgrade.py 自己生成的有效检查点才可使用相同参数及 --apply --resume 恢复。没有该检查点时须人工恢复与核对备份，不能创建或伪造检查点。" >&2
      exit 1
      ;;
    *)
      echo "数据库版本不受支持，拒绝修改环境或启动服务；请检查数据库并制定恢复方案。" >&2
      exit 1
      ;;
  esac
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
    guard_billing_upgrade
    sync_release
    validate
    compose pull
    compose up -d --wait --remove-orphans
    compose ps
    ;;
  update)
    require_docker
    validate
    guard_billing_upgrade
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
