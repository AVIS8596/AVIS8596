# PoC：ERC20Bridge 余额守卫重入不变量测试

针对 Arbitrum `nitro-contracts` 审计报告中 `reentrancy-balance` 发现的**性质验证**。
测试直接打在**真实合约** `src/bridge/ERC20Bridge.sol` 上（经仓库的代理助手部署）。

## 验证的性质

> 经由 outbox 的一次 `executeCall`，Bridge 释放的 native token 数量**不得超过显式
> 声明的 `value`**——即 `to.call(data)` 这个外呼不能被用来额外抽走托管资产。

守卫实现（`_executeLowLevelCall`）：在 `to.call(data)` 前后读 Bridge 的 native token
余额，若 `after < before` 则 `revert CallNotAllowed()`。

## 测试内容

`ERC20BridgeReentrancyInvariant.t.sol`：

| 测试 | 断言 | 说明 |
|------|------|------|
| `test_guardBlocks_reentrantOutboxExtraWithdraw` | revert `CallNotAllowed` | 恶意/被攻陷的 **outbox** 想在 data 窗口内重入多抽一笔 → 余额下降被守卫拦截，整笔回滚 |
| `test_accessControl_nonOutboxCannotReenter` | 仅 `value` 流出 | 非 outbox 的 `to` 重入 `executeCall` → `NotOutbox`，无法额外抽取 |
| `test_cannotCallNativeToken` | revert `CallTargetNotAllowed` | 入口禁止 `to == nativeToken` 这道额外防线 |
| `invariant_outflowNeverExceedsDeclared` | `ESCROW - balance == declaredOut` | fuzz 对抗序列：handler 与 attacker 都是 outbox，attacker 在每次 data 外呼里尝试重入多抽；断言 Bridge 净流出恒等于累计声明的 value |

## 结果

```
[PASS] test_accessControl_nonOutboxCannotReenter
[PASS] test_cannotCallNativeToken
[PASS] test_guardBlocks_reentrantOutboxExtraWithdraw
[PASS] invariant_outflowNeverExceedsDeclared (runs: 256, calls: 16384, reverts: 0)
```

**结论**：在 256×64 的 fuzz 序列下，对抗性重入**无法**绕过余额守卫额外抽走托管资产。
与报告的人工分诊一致——该守卫是有意为之且有效的。

### 负控制（证明测试非空洞）

把守卫条件改成 `if (false)`（即移除守卫）后重跑，不变量**立即失败**，Foundry 收缩出
最小反例：单次 `execute(value=81, extra=60, withData=true)`——重入 outbox 多抽了 60。
这证明：(1) 不变量确实能捕获抽空；(2) 该守卫是防住这条攻击路径的关键。

## 如何复现

本测试依赖 nitro-contracts 的构建上下文（remappings / node_modules / 代理助手），
需放进该仓库里跑：

```bash
source audit-lab/env.sh

# 1) 准备目标仓库（与首轮报告相同）
git clone https://github.com/OffchainLabs/nitro-contracts
cd nitro-contracts
git submodule update --init --recursive
yarn install --ignore-scripts --registry https://registry.npmjs.org

# 2) 放入 PoC
cp ../audit-lab/pocs/ERC20BridgeReentrancyInvariant.t.sol test/foundry/

# 3) 运行（test profile；守卫路径需要）
export FOUNDRY_OFFLINE=true FOUNDRY_PROFILE=test
export FOUNDRY_INVARIANT_RUNS=256 FOUNDRY_INVARIANT_DEPTH=64 FOUNDRY_INVARIANT_FAIL_ON_REVERT=false
forge test --match-path test/foundry/ERC20BridgeReentrancyInvariant.t.sol -vv
```

> 注：solc 0.8.17 已由 `audit-lab/setup.sh` 离线预置；`FOUNDRY_PROFILE=test` 是必须的，
> 因为仓库默认 profile 用 `skip=['test/*']` 跳过测试目录。
