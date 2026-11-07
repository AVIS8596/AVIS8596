# Arbitrum (nitro-contracts) 静态分析首轮报告

> ⚠️ 这是用 Slither 做的**自动化首轮静态分析 + 人工分诊**，不是完整人工审计。
> 仅用于授权范围内的安全研究与负责任披露。

## 1. 审计对象

| 项 | 值 |
|----|----|
| 仓库 | `OffchainLabs/nitro-contracts` |
| 版本 | `@arbitrum/nitro-contracts` v3.2.0 |
| commit | `238c52fa259bee439b0274c76f2a98f67f5e07d9` |
| 范围 | `src/` 下 140 个业务合约（排除 mocks / test-helpers） |
| 编译 | solc 0.8.17 / optimizer 2000 / evm london（离线） |
| 工具 | Foundry 1.5.1 + Slither 0.11.6 |

## 2. 方法

1. `git clone` + `git submodule update --init`（forge-std、precompiles）
2. `yarn install`（OpenZeppelin / Uniswap / upgrade-executor 等 remapping 依赖）
3. `forge build` —— 305 个文件编译通过（仅 1 个 mock 的 benign warning）
4. `slither .` —— 沿用仓库自带 `slither.config.json`：
   - `exclude_dependencies / informational / low / optimization = true`
   - `filter_paths = test-helpers | mocks | node_modules`
   - 同时自动加载仓库的 `slither.db.json`（团队已分诊/抑制的历史结果）

> 复现：`bash audit-lab/setup.sh && source audit-lab/env.sh`，然后在目标仓库
> `forge build && slither .`。

## 3. 结果总览

`301 个合约 / 59 个检测器`，过滤后**剩余 2 条 High 影响、Medium 置信**的发现。
二者经人工分诊均为**设计如此 / 误报**。这对一条经过多轮专业审计的 L2 来说是
预期结果——噪声已被配置和 triage DB 清掉，剩下的是需要结合上下文判断的模式。

| # | 检测器 | 影响/置信 | 位置 | 分诊结论 |
|---|--------|-----------|------|----------|
| 1 | arbitrary-send-eth | High / Medium | `src/stylus/StylusDeployer.sol:78` | 误报（部署器把调用者自己的钱转给其刚部署的合约）|
| 2 | reentrancy-balance | High / Medium | `src/bridge/ERC20Bridge.sol:111` | 设计如此（前后余额差是防护性校验）|

## 4. 逐条分诊

### [1] StylusDeployer.deploy — arbitrary-send-eth（误报）

```solidity
address newContractAddress = deployContract(bytecode, salt);   // 本函数刚 CREATE/CREATE2 出来的地址
...
(bool success, bytes memory data) =
    address(newContractAddress).call{value: initValue}(initData);  // ← Slither 标记
...
uint256 bal = msg.value - dataFee - initValue;   // 0.8 下负数会 revert
if (bal != 0) { (bool sent,) = payable(msg.sender).call{value: bal}(""); ... }
```

- 收款方 `newContractAddress` **不是任意地址**，而是本次调用用调用者提供的
  `bytecode` 刚部署出的合约。
- 转的是调用者自己的 `initValue`（`≤ msg.value`，否则 `msg.value - initValue`
  在计算 `activationValue` 时下溢 revert）。
- 合约不在调用间托管他人 eth，结尾把余额退回 `msg.sender`，且退款额受自身
  `msg.value` 约束，无法提取他人/历史资金。

**结论**：工厂/部署器合约的典型 Slither 误报，无资金损失路径。

### [2] ERC20Bridge._executeLowLevelCall — reentrancy-balance（设计如此）

```solidity
if (data.length > 0) {
    uint256 bridgeBalanceBefore = IERC20(_nativeToken).balanceOf(address(this));
    (success, returnData) = to.call(data);                 // ← Slither 标记
    uint256 bridgeBalanceAfter = IERC20(_nativeToken).balanceOf(address(this));
    if (bridgeBalanceAfter < bridgeBalanceBefore) revert CallNotAllowed();  // 防护
}
```

- 这段前后读余额**本身就是防护逻辑**：禁止这次外呼净减少 Bridge 托管的 native
  token（防止被用来盗取托管资产）。Slither 命中的"调用前读、调用后用"正是该
  校验的形态。
- 入口 `_executeLowLevelCall` 受上层 `onlyOutbox` 等授权约束，并非任意可重入。

**结论**：有意为之的余额守卫，非漏洞。可留意的理论边界：若重入路径在外呼中向
Bridge 回补 native token 以掩盖净流出，差值校验可能被绕过——属已知设计权衡，
需结合 Outbox 授权与具体 token 行为判断，建议用不变量测试覆盖（见下）。

## 5. 静态分析的盲区——L2 真正的风险在哪

Slither 擅长通用 EVM 反模式（重入、溢出、tx.origin…），但 **L2 的高危漏洞通常在
它看不到的业务逻辑层**，需要人工 + 模糊/不变量测试：

- **跨域消息**：L1↔L2 inbox/outbox 的消息编码、重放、授权（Bridge/Inbox/Outbox）
- **欺诈证明 / 状态结算**：`challengeV2` / `osp`（one-step proof）/ `rollup` 的
  断言、质押、挑战时序与经济安全
- **费用与 gas 记账**：ERC20 fee token、retryable ticket 提交费（本次见到的
  `_baseFeeToReport` 返回 0 即与此相关）
- **升级与权限**：`UpgradeExecutor`、代理可升级合约的初始化/权限矩阵

### 建议的下一步（用已搭好的 Foundry）

```bash
# 1) 跑项目自带测试（含 fuzz，foundry.toml 里 runs=1000）
forge test

# 2) 针对 Bridge 余额守卫写不变量测试（invariant）：
#    "任何 outbox 发起的外呼后，Bridge 的 native token 余额不减少"
forge test --match-test invariant_ -vvv

# 3) 分叉主网/测试网做场景复现：
anvil --fork-url <ARB_RPC>   # 另开一个终端
cast / forge script 连上去验证具体攻击假设
```

## 6. 负责任披露

Arbitrum 有官方漏洞赏金，**不要在主网拿真实资金做实验**：

- **Immunefi — Arbitrum**：协议/合约漏洞赏金（历史最高档达数百万美元）
- 官方安全联系：见 nitro-contracts 仓库 `SECURITY` / README 指引
- 披露前在本地分叉或测试网完成 PoC，走平台授权流程提交

## 7. 补充验证：ERC20Bridge 余额守卫不变量测试（已完成）

针对第 4 节 [2] 的 `reentrancy-balance`，对**真实 `ERC20Bridge`** 写了 Foundry
不变量 + PoC（见 `audit-lab/pocs/`）验证性质：

> 经由 outbox 的一次 `executeCall`，Bridge 释放的 native token 不得超过声明的 `value`。

```
[PASS] test_guardBlocks_reentrantOutboxExtraWithdraw   // 恶意 outbox 重入多抽 -> CallNotAllowed
[PASS] test_accessControl_nonOutboxCannotReenter        // 非 outbox 重入 -> NotOutbox
[PASS] test_cannotCallNativeToken                       // to==nativeToken -> CallTargetNotAllowed
[PASS] invariant_outflowNeverExceedsDeclared  (runs: 256, calls: 16384, reverts: 0)
```

- **结论**：256×64 的对抗性 fuzz 序列下，重入**无法**绕过守卫额外抽走托管资产，
  与人工分诊一致——守卫有意为之且有效。
- **负控制**：把守卫改成 `if (false)` 后不变量立即失败，最小反例
  `execute(value=81, extra=60)`（重入 outbox 多抽 60），证明测试非空洞、守卫是关键。

> 复现步骤见 `audit-lab/pocs/README.md`。

---
*本报告由自动化工具 + 人工分诊生成，结论基于上述 commit 的源码静态分析，不构成
对合约安全性的完整保证。*
