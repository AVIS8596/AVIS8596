# Arbitrum express-lane-auction 人工复核

> 目标:Timeboost 时间拍卖模块(较新,审计相对少)。方法:Slither 打猎模式(保留 low)
> + 对 573 行主合约及辅助库的人工逻辑复核。commit `238c52f`。

## 结论

**未发现可报(bidder 可利用)的漏洞。** 非特权参与者(竞拍者)的攻击面均已妥善处理;
剩下的是**信任模型观察**,不构成 bounty 级别的合约漏洞。

## 核对的攻击面

| 攻击类别 | 结论 | 依据 |
|---------|------|------|
| 竞价签名跨链/跨轮/跨合约重放 | 已防 | EIP-712 域含 chainId + verifyingContract(`__EIP712_init`),bid 结构含 `round`;域分隔完整 |
| 同一余额双花(边投标边提现) | 已防 | 两步提现(初始化 r → 终结 r+2);`balanceAtRound` 与 `reduce`/`recoverAndCheckBalance` 用一致的 round 参数,到期轮余额归零 |
| 重复/回溯结算某轮 | 已防 | `setResolvedRound` 要求轮号严格递增,已结算 revert `RoundAlreadyResolved` |
| 二价计算把赢家多扣 | 已防 | `priceToPay = secondPriceBid.amount ≤ firstPriceBid.amount`;赢家付的永远 ≤ 自己签名承诺的 amount |
| 第二名余额被误用 | 已防 | 强制 `firstPriceBidder != secondPriceBidder`,各自独立查余额 |
| ELC 控制权越权转移 | 已防 | `transferExpressLaneController` 仅当前/未来已结算轮,且须是 ELC 本人或其 transferor |
| timing-setter 回溯改轮 | 已防 | `setRoundTimingInfo` 要求当前轮号与下一轮起点不变 |
| 余额下溢 | 不可能 | `balanceAtRound = balance - (0 或 balance)`,恒非负 |
| `roundDuration` 设成巨值锁死余额 | 已防 | `roundDurationSeconds > 1 days` revert |

## 信任模型观察(非漏洞)

- **auctioneer 是受信方**(AUCTIONEER_ROLE,链下自治拍卖器)。恶意 auctioneer 可以用一个
  自己控制、余额充足的"第二名"把二价抬到等于赢家出价,使赢家付到其签名上限。但这**仍在
  赢家签名授权的 amount 之内**,合约保证绝不超过——属于拍卖器的信任假设,不是合约缺陷。
- biddingToken 由治理设定;若选了带回调的 token 理论上可重入,但那是选币的信任问题。

## 评价

该模块设计扎实:签名域分隔、两步提现、单轮单次结算、二价上界,这些关键不变量都成立。
与核心 Outbox 一样,经典陷阱都被堵死。对竞拍者而言无可利用路径。

> 这正是审计成熟代码的常态:大多数模块的诚实结论是"稳健"。真正可付费的漏洞极其罕见。
