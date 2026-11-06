#!/usr/bin/env bash
# 一键重建 Foundry + Slither 本地审计环境。
#
# 为什么不是官方安装器：本云端会话的出站代理禁止访问
#   - foundry.paradigm.xyz  (foundryup 安装器)
#   - binaries.soliditylang.org  (forge / solc-select 默认下载 solc 的地方)
# 但 GitHub Releases 资源 (github.com/.../releases/download/...) 是放行的，
# 所以这里直接从 GitHub Release 拿预编译二进制，然后手动摆放到
# forge(svm) 和 slither(solc-select) 各自查找的目录里，实现完全离线编译。
#
# 幂等：可重复运行。
set -euo pipefail

HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
FOUNDRY_DIR="${FOUNDRY_DIR:-$HOME/.foundry}"
SVM_DIR="${SVM_DIR:-$HOME/.svm}"
# 要预置的 solc 版本（按需增减）
SOLC_VERSIONS=("0.8.26" "0.8.19")

log() { printf '\033[1;32m==>\033[0m %s\n' "$*"; }

# ---------------------------------------------------------------------------
# 1) Foundry (forge / cast / anvil / chisel)
# ---------------------------------------------------------------------------
if [ -x "$FOUNDRY_DIR/bin/forge" ]; then
  log "Foundry 已存在: $("$FOUNDRY_DIR/bin/forge" --version | head -1)"
else
  log "下载 Foundry stable ..."
  mkdir -p "$FOUNDRY_DIR/bin"
  tmp="$(mktemp -d)"
  curl -fsSL --retry 3 -o "$tmp/foundry.tar.gz" \
    "https://github.com/foundry-rs/foundry/releases/download/stable/foundry_stable_linux_amd64.tar.gz"
  tar -xzf "$tmp/foundry.tar.gz" -C "$FOUNDRY_DIR/bin"
  rm -rf "$tmp"
  log "Foundry 安装完成: $("$FOUNDRY_DIR/bin/forge" --version | head -1)"
fi
export PATH="$FOUNDRY_DIR/bin:$PATH"

# ---------------------------------------------------------------------------
# 2) Python venv + Slither + solc-select
# ---------------------------------------------------------------------------
if [ ! -d "$HERE/.venv" ]; then
  log "创建 Python venv ..."
  python3 -m venv "$HERE/.venv"
fi
# shellcheck disable=SC1091
. "$HERE/.venv/bin/activate"
if ! command -v slither >/dev/null 2>&1; then
  log "安装 slither-analyzer + solc-select ..."
  pip install --quiet --upgrade pip
  pip install --quiet slither-analyzer solc-select
fi
log "Slither: $(slither --version 2>&1)"

# ---------------------------------------------------------------------------
# 3) solc 二进制 -> 同时摆放到 svm(forge) 和 solc-select(slither)
#    solc-select 在 venv 激活时用 $VIRTUAL_ENV/.solc-select
# ---------------------------------------------------------------------------
SS_DIR="${VIRTUAL_ENV}/.solc-select"
mkdir -p "$SS_DIR/artifacts"
for VER in "${SOLC_VERSIONS[@]}"; do
  svm_bin="$SVM_DIR/$VER/solc-$VER"
  ss_bin="$SS_DIR/artifacts/solc-$VER/solc-$VER"
  if [ -x "$svm_bin" ] && [ -x "$ss_bin" ]; then
    log "solc $VER 已就位"
    continue
  fi
  log "下载 solc $VER (native static) ..."
  tmp="$(mktemp -d)"
  curl -fsSL --retry 3 -o "$tmp/solc" \
    "https://github.com/ethereum/solidity/releases/download/v$VER/solc-static-linux"
  chmod +x "$tmp/solc"
  mkdir -p "$SVM_DIR/$VER" "$SS_DIR/artifacts/solc-$VER"
  cp "$tmp/solc" "$svm_bin";  chmod +x "$svm_bin"
  cp "$tmp/solc" "$ss_bin";   chmod +x "$ss_bin"
  rm -rf "$tmp"
done
# 设 solc-select 默认版本
echo "${SOLC_VERSIONS[0]}" > "$SS_DIR/global-version"
log "solc-select 可用版本:"; solc-select versions

log "完成。用法:  source $HERE/env.sh"
