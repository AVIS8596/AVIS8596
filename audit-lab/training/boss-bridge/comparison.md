# Boss Bridge — 对照官方答案(步骤 3 校准)

> 流程:先独立审完写 `independent-report.md`(未看答案),再拉官方发现对照。
> 这里诚实记录命中 / 低估 / 漏报,这才是这个练习的价值。

## 对照表

| 官方 | 我的独立报告 | 判定 |
|------|------|------|
| [H-1] 任意 `from` 盗取已授权代币 | H-1(+PoC) | ✅ 命中 |
| [H-2] 金库自存 → L2 无限增发 | H-2 | ✅ 命中 |
| [H-3] 提现签名可重放(无 nonce) | H-3(+PoC,并额外点出无 chainId 的跨链重放) | ✅ 命中 |
| [H-4] `sendToL1` 任意调用 → 调 `approveTo` 抽干 | H-4(指出"任意 target.call,权限过大,可调 vault.approveTo/任意函数") | ✅ 命中 |
| [H-5] `TokenFactory` 用 `CREATE` 在 zkSync 失效,应 `CREATE2` | M-1(发现了 zkSync CREATE 问题 + 未检查返回值) | ⚠️ 命中但**定级偏低**(我给 Med/Low,官方 High) |
| [M-1] 提现 `return-bomb` → 无界 gas 消耗 | 未提 | ❌ **漏报** |
| [L-1] 提现成功未 emit 事件 | 仅提到 `Deposit` 参数未 indexed,未点到提现侧无事件 | ◑ 擦边/未精确命中 |
| [I-1] 测试覆盖不足(L1Vault 0%) | 未提 | ❌ 漏(信息级) |

## 两个 PoC(已跑通,`AuditPoC.t.sol`)

- `test_H1_arbitraryFrom_stealsApprovedTokens`:攻击者用受害者对桥的授权,把受害者
  全部余额拉进金库,并在 L2 记到攻击者名下。
- `test_H3_signatureReplay_drainsVault`:一个 signer 的合法提现签名被**重放 5 次**,
  金库被按 5×amount 放血。

## 校准结论(诚实的"成绩单")

- **强项**:4 个直接导致资金损失的 High(H-1~H-4)全部独立命中,且其中 2 个有可跑通 PoC。
  这是最值钱的部分——逻辑型、可盗资金的漏洞。
- **弱项 1 — 定级**:H-5 的 zkSync `CREATE` 问题我发现了但低估为 Med/Low;
  跨链部署在本项目是明确目标(README 说要上 zkSync),应按 High 记。
  教训:**定级要结合项目的部署/使用上下文**,不能只看单合约。
- **弱项 2 — gas/DoS 类漏报**:M-1 的 return-bomb(外部 call 返回超大 returndata 撑爆 gas)
  我完全没想到。教训:**除了"能不能偷钱",还要专门过一遍 gas griefing / DoS 向量**
  (外部 call 的 returndata、无界循环、强制 revert 等)。
- **弱项 3 — 完整性/信息项**:提现无事件(L-1)、测试覆盖(I-1)这类"非盗币但影响
  可维护性/可监控性"的项容易被逻辑向审计忽略。真实竞赛里这些也能拿分。

> 一句话:**我这类"逻辑向"审计者的典型画像——抓资金漏洞准,但容易漏 gas/DoS 和
> 完整性项,且定级会受单合约视角局限。** 知道自己的盲区,就是下一步要刻意补的地方。
