#!/bin/bash
# =============================================================================
# tradeinvestors-core: provision.sh
# Run once as root on a fresh Ubuntu 24.04 VPS.
# Idempotent — safe to run again; each step checks before acting.
#
# Two-phase run:
#   Phase 1 (first run):  installs system deps, generates deploy key, exits.
#   Phase 2 (second run): DEPLOY_KEY_ADDED=true — clones repo, sets up app.
# =============================================================================
set -euo pipefail

# ── Pinned versions (must match srv957297) ────────────────────────────────────
PYTHON_VERSION="3.11.9"
IBC_VERSION="3.23.0"
IBGATEWAY_MAJOR="1045"
JAVAFX_VERSION="17.0.13"

IBGATEWAY_URL="https://download2.interactivebrokers.com/installers/ibgateway/stable-standalone/ibgateway-stable-standalone-linux-x64.sh"
IBC_URL="https://github.com/IbcAlpha/IBC/releases/download/${IBC_VERSION}/IBCLinux-${IBC_VERSION}.zip"
JAVAFX_URL="https://download2.gluonhq.com/openjfx/${JAVAFX_VERSION}/openjfx-${JAVAFX_VERSION}_linux-x64_bin-sdk.zip"
PYTHON_URL="https://www.python.org/ftp/python/${PYTHON_VERSION}/Python-${PYTHON_VERSION}.tar.xz"

# ── Parameters ────────────────────────────────────────────────────────────────
IBKR_USER="${IBKR_USER:-}"
IBKR_PASS="${IBKR_PASS:-}"
IB_ACCOUNT="${IB_ACCOUNT:-}"
IB_PORT="${IB_PORT:-4003}"
IB_CLIENT_ID="${IB_CLIENT_ID:-1}"
SOLO_TRADER_SECRET="${SOLO_TRADER_SECRET:-}"
OPS_API_KEY="${OPS_API_KEY:-}"
CLIENT_ID="${CLIENT_ID:-}"
DEPLOY_KEY_ADDED="${DEPLOY_KEY_ADDED:-false}"

usage() {
  echo ""
  echo "Phase 1 (generate deploy key):"
  echo "  IBKR_USER=u IBKR_PASS=p IB_ACCOUNT=DU123 SOLO_TRADER_SECRET=s OPS_API_KEY=k CLIENT_ID=slug bash provision.sh"
  echo ""
  echo "Phase 2 (after adding deploy key to GitHub):"
  echo "  DEPLOY_KEY_ADDED=true IBKR_USER=u IBKR_PASS=p IB_ACCOUNT=DU123 SOLO_TRADER_SECRET=s OPS_API_KEY=k CLIENT_ID=slug bash provision.sh"
  echo ""
  echo "Optional: IB_PORT=4003  IB_CLIENT_ID=1"
  echo ""
  exit 1
}

for var in IBKR_USER IBKR_PASS IB_ACCOUNT SOLO_TRADER_SECRET OPS_API_KEY CLIENT_ID; do
  if [[ -z "${!var:-}" ]]; then
    echo "ERROR: $var is required"
    usage
  fi
done

# ── Paths ─────────────────────────────────────────────────────────────────────
# BASE_DIR   = install root (gateway/, ibc/, scripts/, .env live here)
# REPO_DIR   = git clone target (repo root inside BASE_DIR)
# APP_DIR    = Python app directory (app/ subdirectory inside the repo)
BASE_DIR="/root/trader"
REPO_DIR="${BASE_DIR}/app"          # /root/trader/app  — repo root
APP_DIR="${REPO_DIR}/app"           # /root/trader/app/app — Python app
GATEWAY_DIR="${BASE_DIR}/gateway"
IBC_DIR="${BASE_DIR}/ibc"
SCRIPTS_DIR="${BASE_DIR}/scripts"
DATA_DIR="/root/ops/instances/${CLIENT_ID}"
DISPLAY_NUM=1

log()  { echo ""; echo "▶ $*"; }
ok()   { echo "  ✓ $*"; }
skip() { echo "  ─ $* (already done)"; }

echo ""
echo "╔══════════════════════════════════════════════╗"
echo "║   TradeInvestors Provisioning Script         ║"
echo "╠══════════════════════════════════════════════╣"
printf "║  CLIENT_ID  : %-31s║\n" "${CLIENT_ID}"
printf "║  IB_ACCOUNT : %-31s║\n" "${IB_ACCOUNT}"
printf "║  IB_PORT    : %-31s║\n" "${IB_PORT}"
printf "║  Phase 2    : %-31s║\n" "${DEPLOY_KEY_ADDED}"
echo "╚══════════════════════════════════════════════╝"
echo ""


# =============================================================================
# 1. System dependencies
# =============================================================================
log "Installing system dependencies..."
apt-get update -qq
apt-get install -y -qq \
  git curl wget unzip \
  xvfb x11vnc openbox \
  build-essential libssl-dev zlib1g-dev \
  libbz2-dev libreadline-dev libsqlite3-dev libffi-dev \
  libncursesw5-dev xz-utils tk-dev libxml2-dev libxmlsec1-dev liblzma-dev \
  openjdk-17-jre \
  > /dev/null
ok "System packages installed"


# =============================================================================
# 2. Python 3.11.9 from source
# =============================================================================
log "Checking Python ${PYTHON_VERSION}..."
if /usr/local/bin/python3.11 --version 2>/dev/null | grep -q "${PYTHON_VERSION}"; then
  skip "Python ${PYTHON_VERSION} already installed"
else
  log "Building Python ${PYTHON_VERSION} from source (this takes ~5 min)..."
  TMPPY=$(mktemp -d)
  wget -q "${PYTHON_URL}" -O "${TMPPY}/python.tar.xz"
  tar -xf "${TMPPY}/python.tar.xz" -C "${TMPPY}"
  cd "${TMPPY}/Python-${PYTHON_VERSION}"
  ./configure --enable-optimizations --with-ensurepip=install -q
  make -j"$(nproc)" -s
  make altinstall -s
  cd /
  rm -rf "${TMPPY}"
  ok "Python ${PYTHON_VERSION} installed at /usr/local/bin/python3.11"
fi


# =============================================================================
# 3. JavaFX 17.0.13
# =============================================================================
log "Checking JavaFX ${JAVAFX_VERSION}..."
JAVAFX_DIR="/usr/local/lib/javafx-sdk-${JAVAFX_VERSION}"
if [[ -d "${JAVAFX_DIR}" ]]; then
  skip "JavaFX ${JAVAFX_VERSION} already at ${JAVAFX_DIR}"
else
  log "Downloading JavaFX ${JAVAFX_VERSION}..."
  TMPJFX=$(mktemp -d)
  wget -q "${JAVAFX_URL}" -O "${TMPJFX}/javafx.zip"
  unzip -q "${TMPJFX}/javafx.zip" -d /usr/local/lib/
  rm -rf "${TMPJFX}"
  ok "JavaFX installed at ${JAVAFX_DIR}"
fi


# =============================================================================
# 4. IB Gateway ${IBGATEWAY_MAJOR} (stable channel)
# =============================================================================
log "Checking IB Gateway ${IBGATEWAY_MAJOR}..."
# The installer creates <GATEWAY_DIR>/app/ibgateway (the launcher binary).
# Use that as the sentinel — it's version-independent and only exists after
# a successful install4j run.
if [[ -f "${GATEWAY_DIR}/app/ibgateway" ]]; then
  skip "IB Gateway already installed at ${GATEWAY_DIR}/app/ibgateway"
else
  log "Downloading IB Gateway (stable, ~320 MB)..."
  mkdir -p "${GATEWAY_DIR}"
  TMPIBGW=$(mktemp -d)
  wget -q "${IBGATEWAY_URL}" -O "${TMPIBGW}/ibgateway-installer.sh"
  chmod +x "${TMPIBGW}/ibgateway-installer.sh"
  bash "${TMPIBGW}/ibgateway-installer.sh" -q -dir "${GATEWAY_DIR}" \
    || bash "${TMPIBGW}/ibgateway-installer.sh" -q -overwrite -dir "${GATEWAY_DIR}"
  rm -rf "${TMPIBGW}"
  ok "IB Gateway installed at ${GATEWAY_DIR}"
fi


# =============================================================================
# 5. IBC 3.23.0
# =============================================================================
log "Checking IBC ${IBC_VERSION}..."
if [[ -f "${IBC_DIR}/IBC.jar" ]] && grep -q "${IBC_VERSION}" "${IBC_DIR}/version" 2>/dev/null; then
  skip "IBC ${IBC_VERSION} already at ${IBC_DIR}"
else
  log "Downloading IBC ${IBC_VERSION}..."
  mkdir -p "${IBC_DIR}"
  TMPIBC=$(mktemp -d)
  wget -q "${IBC_URL}" -O "${TMPIBC}/IBC.zip"
  unzip -q -o "${TMPIBC}/IBC.zip" -d "${IBC_DIR}"
  chmod +x "${IBC_DIR}"/*.sh "${IBC_DIR}/scripts"/*.sh 2>/dev/null || true
  echo "${IBC_VERSION}" > "${IBC_DIR}/version"
  rm -rf "${TMPIBC}"
  ok "IBC ${IBC_VERSION} installed at ${IBC_DIR}"
fi


# =============================================================================
# 6. Deploy key
# =============================================================================
log "Setting up deploy key..."
DEPLOY_KEY_PATH="/root/.ssh/deploy_key_${CLIENT_ID}"
mkdir -p /root/.ssh
chmod 700 /root/.ssh

if [[ -f "${DEPLOY_KEY_PATH}" ]]; then
  skip "Deploy key already exists at ${DEPLOY_KEY_PATH}"
else
  ssh-keygen -t ed25519 -C "deploy-${CLIENT_ID}@$(hostname)" -f "${DEPLOY_KEY_PATH}" -N ""
  ok "Deploy key generated at ${DEPLOY_KEY_PATH}"
fi

# ── Phase 1 exit ─────────────────────────────────────────────────────────────
# If DEPLOY_KEY_ADDED is not set, print the key and exit so the operator can
# add it to GitHub before re-running. This keeps human interaction between
# two clean script runs rather than in the middle of one.
if [[ "${DEPLOY_KEY_ADDED}" != "true" ]]; then
  echo ""
  echo "╔══════════════════════════════════════════════════════════════════╗"
  echo "║   PHASE 1 COMPLETE — ACTION REQUIRED BEFORE PHASE 2             ║"
  echo "╠══════════════════════════════════════════════════════════════════╣"
  echo "║  System deps, Python, JavaFX, IBGateway, and IBC are installed. ║"
  echo "║  A deploy key has been generated. Add it to GitHub, then        ║"
  echo "║  re-run with DEPLOY_KEY_ADDED=true to finish provisioning.      ║"
  echo "╚══════════════════════════════════════════════════════════════════╝"
  echo ""
  echo "  1. Add this read-only deploy key to GitHub:"
  echo "     https://github.com/mayank-jain632/tradeinvestors-core/settings/keys"
  echo ""
  echo "── Public key ─────────────────────────────────────────────────────"
  cat "${DEPLOY_KEY_PATH}.pub"
  echo "───────────────────────────────────────────────────────────────────"
  echo ""
  echo "  2. Then re-run (paste all original vars, add DEPLOY_KEY_ADDED=true):"
  echo ""
  echo "     DEPLOY_KEY_ADDED=true \\"
  echo "     IBKR_USER=${IBKR_USER} \\"
  echo "     IBKR_PASS='*****' \\"
  echo "     IB_ACCOUNT=${IB_ACCOUNT} \\"
  echo "     IB_PORT=${IB_PORT} \\"
  echo "     IB_CLIENT_ID=${IB_CLIENT_ID} \\"
  echo "     CLIENT_ID=${CLIENT_ID} \\"
  echo "     SOLO_TRADER_SECRET='*****' \\"
  echo "     OPS_API_KEY='*****' \\"
  echo "     bash provision.sh"
  echo ""
  exit 0
fi

# ── Phase 2 continues from here ──────────────────────────────────────────────

# Add SSH host alias (guarded against duplicates on re-run)
if ! grep -q "Host github-${CLIENT_ID}" /root/.ssh/config 2>/dev/null; then
  cat >> /root/.ssh/config << SSHCFG

# Auto-added by provision.sh for ${CLIENT_ID}
Host github-${CLIENT_ID}
  HostName github.com
  User git
  IdentityFile ${DEPLOY_KEY_PATH}
  IdentitiesOnly yes
SSHCFG
  ok "SSH config entry added for github-${CLIENT_ID}"
else
  skip "SSH config entry already present"
fi


# =============================================================================
# 7. Clone repo into /root/trader/app (or pull if already present)
# =============================================================================
log "Syncing repo from GitHub..."
if [[ -d "${REPO_DIR}/.git" ]]; then
  cd "${REPO_DIR}"
  git pull
  ok "Repo already present at ${REPO_DIR} — pulled latest"
else
  mkdir -p "${BASE_DIR}"
  git clone "git@github-${CLIENT_ID}:mayank-jain632/tradeinvestors-core.git" "${REPO_DIR}"
  ok "Repo cloned to ${REPO_DIR}"
fi


# =============================================================================
# 8. Python venv + dependencies
# =============================================================================
log "Setting up Python venv..."
mkdir -p "${DATA_DIR}" "${SCRIPTS_DIR}"

if [[ ! -f "${APP_DIR}/venv/bin/activate" ]]; then
  /usr/local/bin/python3.11 -m venv "${APP_DIR}/venv"
  ok "venv created at ${APP_DIR}/venv"
else
  skip "venv already exists"
fi
"${APP_DIR}/venv/bin/pip" install --quiet --upgrade pip
"${APP_DIR}/venv/bin/pip" install --quiet -r "${APP_DIR}/requirements.txt"
ok "Python dependencies installed"


# =============================================================================
# 9. IBC config.ini
# =============================================================================
log "Writing IBC config.ini..."
IBC_INI="${IBC_DIR}/config.ini"
cp "${REPO_DIR}/ibc/config.ini" "${IBC_INI}"
sed -i "s|^IbLoginId=.*|IbLoginId=${IBKR_USER}|"   "${IBC_INI}"
sed -i "s|^IbPassword=.*|IbPassword=${IBKR_PASS}|" "${IBC_INI}"
sed -i "s|^IbDir=.*|IbDir=${GATEWAY_DIR}|"         "${IBC_INI}"
ok "IBC config.ini written to ${IBC_INI}"


# =============================================================================
# 10. IBC gatewaystart.sh — patch paths
# =============================================================================
log "Configuring IBC gatewaystart.sh..."
GATEWAY_SH="${IBC_DIR}/gatewaystart.sh"
if [[ -f "${GATEWAY_SH}" ]]; then
  sed -i "s|^TWS_MAJOR_VRSN=.*|TWS_MAJOR_VRSN=${IBGATEWAY_MAJOR}|" "${GATEWAY_SH}"
  sed -i "s|^TWS_PATH=.*|TWS_PATH=${GATEWAY_DIR}|"                  "${GATEWAY_SH}"
  sed -i "s|^IBC_INI=.*|IBC_INI=${IBC_INI}|"                       "${GATEWAY_SH}"
  sed -i "s|^IBC_PATH=.*|IBC_PATH=${IBC_DIR}|"                     "${GATEWAY_SH}"
  sed -i "s|^LOG_PATH=.*|LOG_PATH=${IBC_DIR}/logs|"                 "${GATEWAY_SH}"
  sed -i "s|^JAVA_PATH=.*|JAVA_PATH=/usr/bin|"                      "${GATEWAY_SH}"
  mkdir -p "${IBC_DIR}/logs"
  chmod +x "${GATEWAY_SH}"
  ok "gatewaystart.sh patched"
else
  echo "  WARNING: ${GATEWAY_SH} not found — IBC zip may have a different structure"
fi


# =============================================================================
# 11. gateway/jts.ini
# =============================================================================
log "Writing gateway jts.ini..."
cp "${REPO_DIR}/gateway/jts.ini" "${GATEWAY_DIR}/jts.ini"
ok "jts.ini written to ${GATEWAY_DIR}/jts.ini"


# =============================================================================
# 12. start-ibgateway.sh (generated, not from repo)
# =============================================================================
log "Writing start-ibgateway.sh..."
cat > "${SCRIPTS_DIR}/start-ibgateway.sh" << STARTSCRIPT
#!/bin/bash
# Auto-generated by provision.sh — do not edit manually
IBC_PATH=${IBC_DIR}
GATEWAY_PATH=${GATEWAY_DIR}
DISPLAY_NUM=${DISPLAY_NUM}

pkill -f "\${IBC_PATH}" 2>/dev/null
pkill -f "Xvfb :\${DISPLAY_NUM}" 2>/dev/null
sleep 2

mv "\${GATEWAY_PATH}/ibgateway/${IBGATEWAY_MAJOR}/ibgateway1" \\
   "\${GATEWAY_PATH}/ibgateway/${IBGATEWAY_MAJOR}/ibgateway" 2>/dev/null || true

Xvfb ":\${DISPLAY_NUM}" -screen 0 1024x768x24 &
export DISPLAY=":\${DISPLAY_NUM}"
sleep 2

cd "\${IBC_PATH}"
while true; do
    mv "\${GATEWAY_PATH}/ibgateway/${IBGATEWAY_MAJOR}/ibgateway1" \\
       "\${GATEWAY_PATH}/ibgateway/${IBGATEWAY_MAJOR}/ibgateway" 2>/dev/null || true
    pkill -f "\${IBC_PATH}" 2>/dev/null
    sleep 2
    bash gatewaystart.sh -inline
    echo "Gateway exited, waiting 60s before restart..."
    sleep 60
done
STARTSCRIPT
chmod +x "${SCRIPTS_DIR}/start-ibgateway.sh"
ok "start-ibgateway.sh written to ${SCRIPTS_DIR}"


# =============================================================================
# 13. .env
# =============================================================================
log "Writing .env..."
cat > "${BASE_DIR}/.env" << ENV
# Generated by provision.sh — $(date -u +"%Y-%m-%d %H:%M UTC")
SOLO_TRADER_SECRET=${SOLO_TRADER_SECRET}
OPS_API_KEY=${OPS_API_KEY}

IB_HOST=127.0.0.1
IB_PORT=${IB_PORT}
IB_CLIENT_ID=${IB_CLIENT_ID}
IB_ACCOUNT=${IB_ACCOUNT}

DRY_RUN=false
DEDUP_WINDOW_SECONDS=300

OPS_DB_PATH=${DATA_DIR}/ops.db
LEDGER_PATH=${DATA_DIR}/expected_positions.json
ENV
chmod 600 "${BASE_DIR}/.env"
ok ".env written to ${BASE_DIR}/.env (mode 600)"


# =============================================================================
# 14. Systemd service files
# =============================================================================
log "Installing systemd services..."

cat > /etc/systemd/system/ibgateway.service << SVC
[Unit]
Description=IB Gateway via IBC (${CLIENT_ID})
After=network.target
Before=solo-trader.service

[Service]
Type=simple
User=root
ExecStart=${SCRIPTS_DIR}/start-ibgateway.sh
Restart=always
RestartSec=30

[Install]
WantedBy=multi-user.target
SVC

cat > /etc/systemd/system/solo-trader.service << SVC
[Unit]
Description=Solo Trader Execution Engine (${CLIENT_ID})
After=network.target ibgateway.service

[Service]
Type=simple
User=root
WorkingDirectory=${APP_DIR}
EnvironmentFile=${BASE_DIR}/.env
ExecStart=${APP_DIR}/venv/bin/uvicorn app:app --host 0.0.0.0 --port 8001
Restart=always
RestartSec=10

[Install]
WantedBy=multi-user.target
SVC

systemctl daemon-reload
systemctl enable ibgateway.service
systemctl enable solo-trader.service
ok "ibgateway.service and solo-trader.service enabled"


# =============================================================================
# 15. UFW firewall
# =============================================================================
log "Configuring firewall..."
if command -v ufw &>/dev/null; then
  ufw allow 8001/tcp comment "solo-trader (${CLIENT_ID})" > /dev/null 2>&1 || true
  ok "ufw: port 8001 allowed"
else
  skip "ufw not installed, skipping"
fi


# =============================================================================
# 16. Log rotation
# =============================================================================
log "Setting up log rotation..."
cat > /etc/logrotate.d/solo-trader << LOGROTATE
${DATA_DIR}/*.log {
    daily
    rotate 14
    compress
    missingok
    notifempty
}
${IBC_DIR}/logs/*.txt {
    daily
    rotate 7
    compress
    missingok
    notifempty
}
LOGROTATE
ok "logrotate configured"


# =============================================================================
# Summary
# =============================================================================
VPS_IP=$(curl -s ifconfig.me 2>/dev/null || echo "YOUR_VPS_IP")

echo ""
echo "╔══════════════════════════════════════════════════════════════╗"
echo "║   Provisioning complete!                                     ║"
echo "╠══════════════════════════════════════════════════════════════╣"
printf "║  CLIENT_ID  : %-47s║\n" "${CLIENT_ID}"
printf "║  IB_ACCOUNT : %-47s║\n" "${IB_ACCOUNT}"
printf "║  IB_PORT    : %-47s║\n" "${IB_PORT}"
printf "║  App dir    : %-47s║\n" "${APP_DIR}"
printf "║  Data dir   : %-47s║\n" "${DATA_DIR}"
echo "╠══════════════════════════════════════════════════════════════╣"
echo "║  NEXT STEPS:                                                 ║"
echo "╚══════════════════════════════════════════════════════════════╝"
echo ""
echo "  1. Start the services:"
echo ""
echo "     systemctl start ibgateway"
echo "     systemctl start solo-trader"
echo ""
echo "  2. Add this instance to the relay's clients.json on the central VPS:"
echo ""
echo "     {\"client_id\": \"${CLIENT_ID}\", \"endpoint\": \"http://${VPS_IP}:8001\", \"active\": true}"
echo ""
echo "  3. Monitor logs:"
echo "     journalctl -u ibgateway -f"
echo "     journalctl -u solo-trader -f"
echo ""
