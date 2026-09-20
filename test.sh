#!/bin/sh
set -eu

ROOT_DIR=$(CDPATH= cd -- "$(dirname -- "$0")" && pwd)
SANDBOX=$(mktemp -d)
trap 'rm -rf "$SANDBOX"' EXIT

set_value() {
  key=$1
  value=$2
  temporary="$SANDBOX/env.tmp"
  awk -v key="$key" -v value="$value" 'BEGIN { FS = "=" } $1 == key { print key "=" value; next } { print }' "$SANDBOX/.env" >"$temporary"
  mv "$temporary" "$SANDBOX/.env"
}

cp "$ROOT_DIR/.env.example" "$SANDBOX/.env"
set_value XBAR_CORE_IMAGE langrentaole/xbar-core:old
set_value CORE_DOMAIN core.test
set_value ACME_EMAIL ops@test.invalid
set_value USER_UPSTREAM http://host.docker.internal:18080
set_value VIBE_GATEWAY_URL https://vibe.test
set_value PAYMENT_MERCHANT_ID disabled
set_value PAYMENT_MERCHANT_KEY 0123456789abcdef0123456789abcdef
set_value PAYMENT_BASE_URL https://payments.test/
set_value MYSQL_PASSWORD 0123456789abcdef0123456789abcdef
set_value MYSQL_ROOT_PASSWORD 0123456789abcdef0123456789abcdef
set_value COOKIE_KEY 0123456789abcdef0123456789abcdef
set_value DATA_KEY 0123456789abcdef0123456789abcdef
set_value VERIFICATION_KEY 0123456789abcdef0123456789abcdef
set_value EDGE_INTERNAL_KEY 0123456789abcdef0123456789abcdef
set_value VIBE_EDGE_SHARED_SECRET 0123456789abcdef0123456789abcdef
set_value ADMIN_USERNAME admin
set_value ADMIN_EMAIL admin@test.invalid
set_value ADMIN_PASSWORD test-password
set_value TIMEZONE Asia/Shanghai

mkdir -p "$SANDBOX/bin"
cat >"$SANDBOX/bin/docker" <<'EOF'
#!/bin/sh
case "$*" in
  *" db mysql "*) [ -n "${FAKE_STATION_ID:-}" ] && printf '%s\n' "$FAKE_STATION_ID" ;;
esac
exit 0
EOF
chmod +x "$SANDBOX/bin/docker"

PATH="$SANDBOX/bin:$PATH" XBAR_CORE_ENV_FILE="$SANDBOX/.env" "$ROOT_DIR/deploy.sh" update >/dev/null
expected_image=$(sed -n 's/^XBAR_CORE_IMAGE=//p' "$ROOT_DIR/.env.example")
actual_image=$(sed -n 's/^XBAR_CORE_IMAGE=//p' "$SANDBOX/.env")
[ "$actual_image" = "$expected_image" ] || {
  echo "update 未同步 Core 镜像版本：$actual_image" >&2
  exit 1
}
[ "$(sed -n 's/^CORE_DOMAIN=//p' "$SANDBOX/.env")" = "core.test" ] || {
  echo "update 不应修改客户域名" >&2
  exit 1
}
[ "$(sed -n 's/^VIBE_EDGE_SHARED_SECRET=//p' "$SANDBOX/.env")" = "0123456789abcdef0123456789abcdef" ] || {
  echo "update 不应修改客户密钥" >&2
  exit 1
}

station_id=$(FAKE_STATION_ID=station-test-id PATH="$SANDBOX/bin:$PATH" XBAR_CORE_ENV_FILE="$SANDBOX/.env" "$ROOT_DIR/deploy.sh" station-id)
[ "$station_id" = "station-test-id" ] || {
  echo "station-id 输出不正确：$station_id" >&2
  exit 1
}
if FAKE_STATION_ID= PATH="$SANDBOX/bin:$PATH" XBAR_CORE_ENV_FILE="$SANDBOX/.env" "$ROOT_DIR/deploy.sh" station-id >/dev/null 2>&1; then
  echo "未授权时 station-id 应失败" >&2
  exit 1
fi

echo "xbar-core-deploy tests passed"
