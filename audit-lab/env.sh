# source 这个文件来激活审计环境：  source audit-lab/env.sh
_HERE="$(cd "$(dirname "${BASH_SOURCE[0]}")" && pwd)"
export PATH="$HOME/.foundry/bin:$PATH"
# shellcheck disable=SC1091
[ -f "$_HERE/.venv/bin/activate" ] && . "$_HERE/.venv/bin/activate"
export FOUNDRY_OFFLINE=true
echo "审计环境已激活: forge $(forge --version 2>/dev/null | head -1 | awk '{print $3}')  |  slither $(slither --version 2>&1)  |  solc $(solc --version 2>/dev/null | grep -o 'Version: [0-9.]*' | awk '{print $2}')"
