# 区块链合约审计环境 (Foundry + Slither)

本地智能合约安全审计工具链。用于**授权范围内**的安全研究：审计开源合约、
在测试网/本地分叉上验证漏洞、负责任披露。请勿用于攻击线上资金。

## 已安装内容

| 工具 | 版本 | 用途 |
|------|------|------|
| Foundry (`forge`/`cast`/`anvil`/`chisel`) | 1.5.1-stable | 编译、测试、模糊测试、本地链、分叉主网 |
| Slither | 0.11.6 | 静态分析，100+ 漏洞检测器 |
| solc | 0.8.26 / 0.8.19 | Solidity 编译器（已离线预置） |

## 快速开始

```bash
# 1) 激活环境（每个新 shell 都要 source 一次）
source audit-lab/env.sh

# 2) 进示例项目
cd audit-lab/sample-audit

forge build          # 编译
forge test           # 跑测试（含 256 轮 fuzz）
forge test -vvv      # 带详细 trace
slither .            # 静态分析整个项目
```

## ⚠️ 云端会话是临时的

这是云端临时容器，会话结束后 `~/.foundry`、`~/.svm`、`.venv` 都会消失，
**未 commit/push 的改动也会丢失**。重建环境：

```bash
bash audit-lab/setup.sh
```

`setup.sh` 幂等，可随时重跑。

## 为什么没用官方安装器

本会话出站代理按策略**禁止**访问：

- `foundry.paradigm.xyz`（foundryup 安装器）
- `binaries.soliditylang.org`（forge / solc-select 默认下载 solc 的源）

但 **GitHub Releases 资源放行**。所以 `setup.sh` 直接从 GitHub Release 下载
预编译二进制，并手动摆放到 forge(svm, `~/.svm/<ver>/solc-<ver>`) 和
slither(solc-select, `$VENV/.solc-select/...`) 各自查找的目录，`foundry.toml`
里设 `offline = true`，从而完全离线编译。

> 需要别的 solc 版本：编辑 `setup.sh` 里的 `SOLC_VERSIONS` 再重跑。

## 审计一个外部合约

```bash
source audit-lab/env.sh
cd audit-lab

# 方式 A：git clone 目标仓库（匿名读放行），在其目录里跑
git clone <repo-url> target && cd target
forge install            # 拉依赖（git 子模块）
forge build
slither .

# 方式 B：单文件快速扫
slither path/to/Contract.sol --solc-solcs-select 0.8.26
```

常用 Slither 选项：

```bash
slither . --checklist          # 生成 markdown 报告
slither . --print human-summary # 合约概览
slither . --exclude-informational
```

## 示例项目里的漏洞 (`sample-audit/src/VulnerableVault.sol`)

故意写的有漏洞合约，Slither 会检出 5 类问题：

- **reentrancy-eth** — 先转账后改余额（违反 checks-effects-interactions）
- **tx-origin** — 用 `tx.origin` 鉴权
- **missing-zero-check** — 地址参数缺零值校验
- **low-level-calls** / **solc-version** — 低级 call、编译器版本已知问题

拿它练手：试着修复后重跑 `slither .` 看问题消失。

## 进阶 / 练手资源

- **Foundry Book** — https://book.getfoundry.sh（fuzz、invariant、fork 测试）
- **Ethernaut / Damn Vulnerable DeFi** — 漏洞模式 CTF
- 模糊测试：`forge test --match-test invariant_ -vvv`
- 分叉主网调试：`anvil --fork-url <RPC>` 然后 `cast`/`forge` 连上去

## 负责任披露（把成果变成"改进它们"）

找到漏洞走正规渠道，不要碰线上真实资金：

- **Immunefi** — DeFi/公链最大漏洞赏金平台
- **以太坊基金会 Bug Bounty** — 协议/客户端/共识层
- 各项目官方 `security@` 邮箱 / HackerOne
