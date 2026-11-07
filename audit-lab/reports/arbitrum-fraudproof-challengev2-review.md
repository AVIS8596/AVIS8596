# Arbitrum 欺诈证明层 (BoLD / challengeV2) 深度复核

> 目标:L2 最复杂、赏金最高的欺诈证明层。方法:对**纯 Solidity 核心**做人工逻辑复核——
> 计时累积与按时确认、bisection、Merkle 累加器前缀/包含证明。commit `238c52f`。
> 范围说明见文末:OSP 的逐条 WASM 语义不在 Solidity-only 复核范围内。

## 结论

**未发现可报漏洞。** 争议博弈的关键安全不变量均成立。这与 BoLD 被多轮顶级审计 +
形式化分析的事实一致。

## 核对的核心不变量

### 1. 按时确认(confirmEdgeByTime)与计时累积
- **一个 mutualId 只能确认一个 edge**:`setConfirmedRival` 对 `confirmedRivals[mutualId]`
  做唯一性检查,已有确认则 revert `RivalEdgeConfirmed`。这是"诚实方必胜"的根基——
  对手边不可能同时被确认。✓
- **unrivaled 时间不可伪造**:`timeUnrivaled` 以 `firstRivals[mutualId]` 记录的首个对手
  创建区块为界;edge 在首个对手之前创建才累计 `firstRivalBlock - createdBlock`,否则为 0。
  首个对手自身恒为 0。一旦被 rival,时间冻结,不会再增长。✓
- **继承计时取 min**:`timeUnrivaledTotal = timeUnrivaled(e) + min(lowerChild, upperChild)`。
  取两子最小值,意味着对手只要在任一分支持续反驳即可阻止确认——瓶颈路径语义正确。✓
- **缓存只增且有上界**:`updateTimerCache` 仅在新值更大时写入,封顶 `uint64.max`;
  `validateCurrentTimer` 防止冗余更新。无法把缓存刷到超过真实 min-path 时间。✓
- **claim 链继承有校验**:`updateTimerCacheByClaim` 经 `checkClaimIdLink` 校验
  `claimId`、`originId==下层 mutualId`、层级恰好相邻,才允许继承上层缓存。✓

### 2. Bisection(bisectEdge)
- 必须 Pending 且 hasRival 才能二分;中点 `mandatoryBisectionHeight` 确定性 →
  对手在相同高度二分、相同子边可合并(merge move)。✓
- 二分点须提供**前缀证明**:`verifyPrefixProof(bisectionRoot, mid+1, endRoot, end+1, ...)`
  证明二分承诺是 end 承诺的前缀。start⊑bisection 由递归二分 + 最终 OSP 兜底(协议级)。✓

### 3. Merkle 累加器(MerkleTreeAccumulatorLib)
- `verifyPrefixProof`:先校验 `root(preExpansion)==preRoot` 且 `treeSize==preSize`;
  再按 `maximumAppendBetween` 的**确定性层级**逐个 append 到 postSize;最后 `root==postRoot`
  且**证明必须被完全消费**(`proofIndex==proof.length`)。无法跳步或塞入多余节点。✓
- `appendCompleteSubTree`:强制 `subtreeRoot != 0`、`level < MAX_LEVEL`、`level < me.length`,
  且 `i < level` 处必须为 0("只能在最低完全子树或其下 append")——杜绝制造"空洞"伪造。✓
- **叶子/内部节点域分隔**:`appendLeaf` / 叶子先 `keccak(32字节)`,内部节点是 `keccak(64字节)`,
  长度不同 → 防二次原像把内部节点当叶子。✓
- `maximumAppendBetween` 里 `1 << (msb) + 1`:Solidity 中 `+` 优先级高于 `<<`,
  实为 `1 << (msb+1)`,掩码正确,非优先级 bug。✓

## 范围与建议

- **未覆盖(Solidity-only 复核做不了)**:OSP 的逐条 WASM/AVM opcode 语义
  (`src/osp/OneStepProver*`)。这是理论上最可能藏 bug 的地方,但它本质是 Go prover 的
  确定性翻译,正确的验证方式是**与 Go 实现做差分/一致性测试**,而非读 Solidity。
  若要继续,这需要搭 WASM 执行的差分测试框架——工作量大、需 Nitro 节点侧配合。
- 争议博弈的 Solidity 逻辑本身:经典陷阱(重复确认、计时伪造、前缀证明伪造、
  叶/节点混淆)都已堵死。

> 诚实评价:这是目前加密领域最硬的代码之一。首轮深挖未得可付费漏洞属预期结果。
