# 训练靶场 #1：Puppy Raffle(CodeHawks / Cyfrin)

> **训练用**。这是故意写了多类真实漏洞的教学合约,有官方标准答案可对照。
> 目的:完整走一遍"工具扫 → 人工审 → 写 PoC → 对答案"的闭环,体会**工具能抓什么、
> 漏什么**,以及真实漏洞长什么样(与 Arbitrum 那种"处处稳健"形成对比)。

- 代码:`Cyfrin/4-puppy-raffle-audit` · `src/PuppyRaffle.sol`(216 行, solc ^0.7.6)
- 工具:Foundry 1.5.1 + Slither 0.11.6 + solc 0.7.6(均由 `audit-lab/setup.sh` 离线预置)

## 发现汇总(对照官方答案,全部命中)

| 级别 | 漏洞 | 位置 | Slither 抓到? | 怎么发现 |
|------|------|------|:---:|------|
| High | **refund 重入**(先转账后清零 players)| `refund` L96-106 | ⚠️ 部分(标为 reentrancy-no-eth / events,非 reentrancy-eth) | 人工 + PoC 确认 |
| High | **弱随机数**(winner/rarity 用 timestamp+difficulty+sender)| `selectWinner` L125 | ✅ weak-prng | 工具 + 人工 |
| High | **totalFees 整数溢出**(uint64,0.7.6 无溢出检查)| L130 / `totalFees` | ❌ **漏** | **纯人工** |
| Medium | **withdrawFees 可被永久锁死**(`balance == totalFees` 严格相等,可被 selfdestruct 强塞 ETH 破坏)| `withdrawFees` L157 | ✅ incorrect-equality | 工具 + 人工 |
| Medium | **enterRaffle O(n²) 去重循环 → gas DoS**(入场越晚越贵,可被 griefing 卡死)| `enterRaffle` L87-92 | ❌ **漏**(只报了 cache-array-length 优化) | **纯人工** |
| Medium | **uint64(fee) 不安全截断** | `selectWinner` L130 | ❌ 漏 | 人工 |
| Low | **getActivePlayerIndex 歧义**(返回 0 既表示"index 0"又表示"不存在")| L116-122 | ❌ 漏 | 人工 |
| Info | pragma 未锁定 / 事件未索引 / magic number 等 | — | ✅ | 工具 |

## 头号漏洞 PoC(已跑通)

`ReentrancyPoC.t.sol` —— 攻击者本金 **1 ETH**,通过重入 `refund` 抽干全池:

```
[PASS] test_reentrancy_drainsRaffle
  attacker net profit (wei): 4000000000000000000   // 净赚 4 ETH = 4 个诚实玩家的入场费
```

根因:`refund` 违反 checks-effects-interactions——
```solidity
payable(msg.sender).sendValue(entranceFee); // ① 先把钱转出去(触发攻击者 receive)
players[playerIndex] = address(0);          // ② 才清零(太晚了)
```
攻击者 `receive()` 在 ① 之后、② 之前反复重入 `refund`,每次都通过
`playerAddress == msg.sender` 检查(因为还没被清零),直到池子见底。

**修复**:先清零再转账(CEI),或加 `nonReentrant`:
```solidity
players[playerIndex] = address(0);
emit RaffleRefunded(playerAddress);
payable(msg.sender).sendValue(entranceFee); // 放最后
```

## 这次训练最重要的一课

Slither 抓到了重入、弱随机、严格相等 DoS,但**漏掉了 3 个真实漏洞**:
整数溢出、O(n²) gas DoS、index-0 歧义。

> **工具负责广度和已知模式,人工负责业务逻辑和工具的盲区。真正值钱的发现,
> 往往是工具漏掉、靠读懂业务逻辑才拿到的那几个。** 竞赛里能跑赢别人的,就是这部分。

## 如何复现

```bash
source audit-lab/env.sh
git clone https://github.com/Cyfrin/4-puppy-raffle-audit puppy-raffle && cd puppy-raffle
git submodule update --init --recursive
# 在 foundry.toml 里加 solc_version="0.7.6" 与 offline=true
cp ../audit-lab/training/puppy-raffle/ReentrancyPoC.t.sol test/
export FOUNDRY_OFFLINE=true
forge test --match-path test/ReentrancyPoC.t.sol -vv
slither .
```

## 另外两个 PoC(已跑通,见 `WeaknessesPoC.t.sol`)

**PoC #2 弱随机数** `test_weakRandomness_attackerAlwaysWins`:
攻击者用和合约一模一样的公式 `keccak(msg.sender, timestamp, difficulty) % n`,
grind 一个让自己中奖的时间戳,只在对自己有利的区块调用 `selectWinner` →
**保证中奖**。三个输入在调用时全部已知/可控,所谓"随机"形同虚设。

**PoC #3 totalFees uint64 溢出** `test_overflow_totalFeesTruncatedAndFeesLocked`:
```
true fee (wei)     : 20000000000000000000   (应得 20 ETH)
recorded totalFees :  1553255926290448384   (实记 ≈1.553 ETH = 20e18 mod 2^64)
```
100 个玩家时 20% 费用 = 20 ETH > uint64.max,`uint64(fee)` 被截断,费用严重低计;
且合约实际持有 20 ETH 费用而 `totalFees` 对不上 → `withdrawFees` 的
`require(balance == totalFees)` 永久失败,**费用被锁死无法取出**。

> 修复:`totalFees` 用 `uint256`;或升级到 solc ≥0.8 并去掉不安全的 `uint64` 转换;
> `withdrawFees` 不要用严格相等做余额校验。

## 小结:Puppy Raffle 三个 High 全部有可跑通 PoC

| PoC | 文件 | 结果 |
|-----|------|------|
| 重入抽干全池 | `ReentrancyPoC.t.sol` | 本金 1 ETH,净赚 4 ETH |
| 弱随机数保证中奖 | `WeaknessesPoC.t.sol` | 攻击者必中 |
| uint64 溢出 + 锁死费用 | `WeaknessesPoC.t.sol` | 20 ETH 记成 1.55 ETH,费用取不出 |

## 进阶路线(1→2→3)

- ✅ **步骤 1**:把 Puppy Raffle 挖透(本目录,三个 PoC 完成)。
- ⏭ **步骤 2**:换下一个 CodeHawks First Flight 靶场,练不同漏洞类型。
- ⏭ **步骤 3**:找一个带已知报告的靶场,**不预看答案**独立出报告再对照,逼近真实竞赛手感。
