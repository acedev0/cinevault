#!/usr/bin/env bash
set -Eeuo pipefail

SERVICE_NAME="cinevault"
SERVICE_USER="cinevault"
INSTALL_ROOT="${CINEVAULT_INSTALL_ROOT:-/opt/cinevault}"
APP_DIR="$INSTALL_ROOT/app"
VENV_DIR="$INSTALL_ROOT/.venv"
DATA_DIR="${CINEVAULT_DATA_DIR:-/var/lib/cinevault}"
REPOSITORY="${CINEVAULT_REPOSITORY:-acedev0/cinevault}"
BRANCH="${CINEVAULT_BRANCH:-main}"
REPOSITORY_URL="https://github.com/${REPOSITORY}.git"
AUTH_ASKPASS=""
SCRIPT_DIR="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"

if [[ -t 1 ]]; then
  RED=$'\033[0;31m'; GREEN=$'\033[0;32m'; BLUE=$'\033[0;36m'; BOLD=$'\033[1m'; RESET=$'\033[0m'
else
  RED=""; GREEN=""; BLUE=""; BOLD=""; RESET=""
fi

info() { printf '%s\n' "${BLUE}●${RESET} $*"; }
success() { printf '%s\n' "${GREEN}✓${RESET} $*"; }
die() { printf '%s\n' "${RED}Error:${RESET} $*" >&2; exit 1; }

cleanup() {
  if [[ -n "$AUTH_ASKPASS" && -f "$AUTH_ASKPASS" ]]; then
    rm -f -- "$AUTH_ASKPASS"
  fi
}
trap cleanup EXIT

configure_private_git_auth() {
  if [[ -n "${GH_TOKEN:-${GITHUB_TOKEN:-}}" ]]; then
    export GH_TOKEN="${GH_TOKEN:-${GITHUB_TOKEN:-}}"
    AUTH_ASKPASS="$(mktemp)"
    cat >"$AUTH_ASKPASS" <<'ASKPASS'
#!/usr/bin/env bash
case "${1:-}" in
  *Username*) printf '%s\n' 'x-access-token' ;;
  *) printf '%s\n' "${GH_TOKEN:?GH_TOKEN is required}" ;;
esac
ASKPASS
    chmod 0700 "$AUTH_ASKPASS"
    export GIT_ASKPASS="$AUTH_ASKPASS"
    export GIT_TERMINAL_PROMPT=0
  fi
}

[[ "${EUID:-$(id -u)}" -eq 0 ]] || die "Run this installer as root (use sudo -i first)."
command -v systemctl >/dev/null 2>&1 || die "This installer requires a systemd-based VPS."

printf '\n%s\n' "${BOLD}CineVault VPS installer${RESET}"
printf '%s\n\n' "Telegram keeps every original. The VPS stores only metadata, thumbnails, and an encrypted login session."

install_packages() {
  if command -v apt-get >/dev/null 2>&1; then
    info "Installing required system packages"
    export DEBIAN_FRONTEND=noninteractive
    apt-get update -y
    apt-get install -y python3 python3-venv python3-pip git curl ca-certificates xz-utils
    apt-get install -y chromium >/dev/null 2>&1 || apt-get install -y chromium-browser >/dev/null 2>&1 || true
  elif command -v dnf >/dev/null 2>&1; then
    info "Installing required system packages"
    dnf install -y python3 python3-pip git curl ca-certificates xz
    dnf install -y chromium >/dev/null 2>&1 || true
  elif command -v yum >/dev/null 2>&1; then
    info "Installing required system packages"
    yum install -y python3 python3-pip git curl ca-certificates xz
    yum install -y chromium >/dev/null 2>&1 || true
  else
    die "Supported package manager not found. Use Ubuntu, Debian, AlmaLinux, or Rocky Linux."
  fi
}

install_packages
configure_private_git_auth

if systemctl list-unit-files "${SERVICE_NAME}.service" >/dev/null 2>&1; then
  systemctl stop "${SERVICE_NAME}.service" >/dev/null 2>&1 || true
fi

if ! id "$SERVICE_USER" >/dev/null 2>&1; then
  useradd --system --home-dir "$DATA_DIR" --create-home --shell /usr/sbin/nologin "$SERVICE_USER"
fi
install -d -m 0755 -o root -g root "$INSTALL_ROOT"
install -d -m 0700 -o "$SERVICE_USER" -g "$SERVICE_USER" "$DATA_DIR" "$DATA_DIR/thumbnails"

ensure_node_runtime() {
  local system_node major architecture archive version temp_dir expected
  system_node="$(command -v node 2>/dev/null || true)"
  major=""
  if [[ -n "$system_node" ]]; then
    major="$($system_node -p 'process.versions.node.split(".")[0]' 2>/dev/null || true)"
  fi
  if [[ "$major" =~ ^[0-9]+$ ]] && (( major >= 18 )); then
    return 0
  fi
  case "$(uname -m)" in
    x86_64|amd64) architecture="x64" ;;
    aarch64|arm64) architecture="arm64" ;;
    *) info "Poster enrichment needs Node.js 18+ on this CPU; using Telegram thumbnails"; return 1 ;;
  esac
  version="v22.23.2"
  archive="node-${version}-linux-${architecture}.tar.xz"
  temp_dir="$(mktemp -d)"
  info "Installing an isolated Node.js runtime for poster enrichment"
  if ! curl -fLsS --proto '=https' --tlsv1.2 "https://nodejs.org/dist/${version}/${archive}" -o "$temp_dir/$archive" \
    || ! curl -fLsS --proto '=https' --tlsv1.2 "https://nodejs.org/dist/${version}/SHASUMS256.txt" -o "$temp_dir/SHASUMS256.txt"; then
    rm -rf -- "$temp_dir"
    info "Node.js download failed; using Telegram thumbnails"
    return 1
  fi
  expected="$(awk -v name="$archive" '$2 == name { print $1 }' "$temp_dir/SHASUMS256.txt")"
  if [[ -z "$expected" ]] || ! printf '%s  %s\n' "$expected" "$temp_dir/$archive" | sha256sum -c - >/dev/null; then
    rm -rf -- "$temp_dir"
    info "Node.js checksum verification failed; using Telegram thumbnails"
    return 1
  fi
  rm -rf -- "$INSTALL_ROOT/.node"
  install -d -m 0755 "$INSTALL_ROOT/.node"
  tar -xJf "$temp_dir/$archive" -C "$INSTALL_ROOT/.node" --strip-components=1
  rm -rf -- "$temp_dir"
}

ensure_node_runtime || true

if [[ -d "$APP_DIR/.git" ]]; then
  info "Updating application files"
  if ! git -C "$APP_DIR" fetch --depth 1 origin "$BRANCH"; then
    die "GitHub download failed. A private repository requires GH_TOKEN or an authenticated Git credential."
  fi
  git -C "$APP_DIR" merge --ff-only FETCH_HEAD
elif [[ -f "$SCRIPT_DIR/pyproject.toml" && -d "$SCRIPT_DIR/cinevault" ]]; then
  if [[ "$SCRIPT_DIR" == "$APP_DIR" ]]; then
    info "Using the installed local CineVault source"
  else
    [[ ! -e "$APP_DIR" ]] || die "$APP_DIR already exists and is not a Git checkout. Move it and run again."
    info "Installing the downloaded CineVault source"
    install -d -m 0755 -o root -g root "$APP_DIR"
    cp -a "$SCRIPT_DIR/." "$APP_DIR/"
  fi
else
  [[ ! -e "$APP_DIR" ]] || die "$APP_DIR exists but is not a CineVault Git checkout. Move it and run again."
  info "Downloading CineVault"
  if ! git clone --depth 1 --branch "$BRANCH" "$REPOSITORY_URL" "$APP_DIR"; then
    die "GitHub download failed. A private repository requires GH_TOKEN or an authenticated Git credential."
  fi
fi

if [[ ! -x "$VENV_DIR/bin/python" ]]; then
  info "Creating the isolated Python environment"
  python3 -m venv "$VENV_DIR"
fi
info "Installing CineVault dependencies"
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check --upgrade pip wheel
"$VENV_DIR/bin/python" -m pip install --disable-pip-version-check -r "$APP_DIR/requirements.txt"
NODE_BIN="$(command -v node 2>/dev/null || true)"
[[ -x "$INSTALL_ROOT/.node/bin/node" ]] && NODE_BIN="$INSTALL_ROOT/.node/bin/node"
NPM_BIN="$(command -v npm 2>/dev/null || true)"
[[ -x "$INSTALL_ROOT/.node/bin/npm" ]] && NPM_BIN="$INSTALL_ROOT/.node/bin/npm"
if [[ -n "$NODE_BIN" && -n "$NPM_BIN" ]]; then
  info "Installing the optional headless poster worker"
  (cd "$APP_DIR" && PATH="$(dirname "$NODE_BIN"):$PATH" "$NPM_BIN" ci --omit=dev --ignore-scripts --no-audit --no-fund) || \
    info "Poster enrichment is unavailable; Telegram thumbnails will still work"
  if [[ ! -x /usr/bin/chromium && -x "$APP_DIR/node_modules/.bin/browsers" ]]; then
    info "Installing an isolated headless Chromium for poster enrichment"
    PATH="$(dirname "$NODE_BIN"):$PATH" "$APP_DIR/node_modules/.bin/browsers" \
      install chrome-headless-shell@stable --path "$INSTALL_ROOT/.browser" >/dev/null 2>&1 || \
      info "Chromium download failed; Telegram thumbnails will remain available"
  fi
fi

chown -R "$SERVICE_USER:$SERVICE_USER" "$DATA_DIR"
chmod 0700 "$DATA_DIR" "$DATA_DIR/thumbnails"

if [[ ! -f "$DATA_DIR/secrets.enc" || "${CINEVAULT_RECONFIGURE:-0}" == "1" ]]; then
  info "Starting interactive Telegram and website setup"
  runuser -u "$SERVICE_USER" -- bash -c "cd '$APP_DIR' && '$VENV_DIR/bin/python' -m cinevault --data-dir '$DATA_DIR' setup"
else
  success "Existing encrypted configuration found; keeping it"
fi

RUNTIME_ENV="$DATA_DIR/runtime.env"
[[ -f "$RUNTIME_ENV" ]] || die "Setup did not create $RUNTIME_ENV."
PORT="$(awk -F= '$1 == "CINEVAULT_PORT" { print $2 }' "$RUNTIME_ENV" | tail -n 1 | tr -d '[:space:]')"
[[ "$PORT" =~ ^[0-9]+$ ]] || die "The saved port is invalid."
(( PORT >= 1024 && PORT <= 65535 )) || die "The saved port is outside 1024-65535."

info "Installing the background service"
cat >"/etc/systemd/system/${SERVICE_NAME}.service" <<UNIT
[Unit]
Description=CineVault Telegram media streaming
After=network-online.target
Wants=network-online.target

[Service]
Type=simple
User=${SERVICE_USER}
Group=${SERVICE_USER}
WorkingDirectory=${APP_DIR}
Environment=CINEVAULT_DATA_DIR=${DATA_DIR}
ExecStart=${VENV_DIR}/bin/uvicorn cinevault.app:create_app --factory --host 0.0.0.0 --port ${PORT} --workers 1 --no-proxy-headers
Restart=on-failure
RestartSec=5
TimeoutStopSec=20
UMask=0077
NoNewPrivileges=true
PrivateTmp=true
ProtectSystem=strict
ProtectHome=true
ProtectKernelTunables=true
ProtectKernelModules=true
ProtectControlGroups=true
RestrictSUIDSGID=true
LockPersonality=true
ReadWritePaths=${DATA_DIR}

[Install]
WantedBy=multi-user.target
UNIT

install -m 0755 "$APP_DIR/scripts/cinevaultctl" /usr/local/bin/cinevaultctl
systemctl daemon-reload
systemctl enable --now "${SERVICE_NAME}.service"

if command -v ufw >/dev/null 2>&1 && ufw status 2>/dev/null | grep -q '^Status: active'; then
  ufw allow "${PORT}/tcp" >/dev/null
  success "Opened TCP port $PORT in UFW"
fi

sleep 2
if ! systemctl is-active --quiet "${SERVICE_NAME}.service"; then
  journalctl -u "${SERVICE_NAME}.service" -n 30 --no-pager >&2 || true
  die "The service did not start. The recent log is shown above."
fi

PUBLIC_IP="$(curl -4fsS --max-time 6 https://api.ipify.org 2>/dev/null || true)"
if [[ -z "$PUBLIC_IP" ]]; then
  PUBLIC_IP="$(hostname -I 2>/dev/null | awk '{print $1}')"
fi
PUBLIC_IP="${PUBLIC_IP:-SERVER-IP}"

printf '\n%s\n' "${GREEN}${BOLD}CineVault is ready.${RESET}"
printf '%s\n' "Open: ${BOLD}http://${PUBLIC_IP}:${PORT}${RESET}"
printf '%s\n\n' "If your VPS provider has a cloud firewall, allow inbound TCP ${PORT}."
printf '%s\n' "Commands:"
printf '%s\n' "  cinevaultctl status"
printf '%s\n' "  cinevaultctl logs"
printf '%s\n' "  sudo cinevaultctl prune"
printf '%s\n' "  sudo cinevaultctl update"
printf '%s\n' "  sudo cinevaultctl mode public"
printf '%s\n' "  sudo cinevaultctl mode private"
printf '%s\n' "  sudo cinevaultctl auto-update enable"
printf '%s\n' "  sudo cinevaultctl uninstall --yes"
