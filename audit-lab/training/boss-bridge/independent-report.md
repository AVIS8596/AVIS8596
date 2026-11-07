# Boss Bridge — 独立审计报告(对照答案之前)

> 步骤 3 练习:**先不看官方答案**,独立审完 194 行 4 个合约后写下的发现。
> 对照结果见 `comparison.md`(事后补)。

## H-1 `depositTokensToL2` 的 `from` 任意 → 盗取任何已授权用户的代币
`depositTokensToL2(address from, address l2Recipient, uint256 amount)` 直接
`token.safeTransferFrom(from, vault, amount)`,`from` 由调用者任意指定。
用户要存款必须先 `approve` 桥合约,于是**任何人**都能用别人的授权:
攻击者调用 `depositTokensToL2(victim, attackerL2, victimBalance)`,把受害者的币
拉进金库,并在 L2 把币记到**攻击者**名下 → 直接盗币。**严重**。

## H-2 用 `from = vault` 自转 → L2 无锁增发(凭空铸币)
金库在构造函数里 `approveTo(bridge, max)`。攻击者调用
`depositTokensToL2(address(vault), attacker, amount)`,`safeTransferFrom(vault, vault, amount)`
是金库到金库的自转(余额不变,仍过 DEPOSIT_LIMIT 检查),却照样 `emit Deposit(vault, attacker, amount)`。
L2 监听该事件给攻击者铸币 —— **没有新锁仓却无限增发 L2 代币**。**严重**。

## H-3 提现签名可重放(无 nonce/deadline/chainId)→ 抽干金库
`withdrawTokensToL1` 构造的签名消息 =
`abi.encode(token, 0, transferFrom(vault, to, amount))`,对同一 `to/amount` **恒定不变**,
`sendToL1` 校验签名后执行,但**没有任何防重放**(无 nonce、无过期、无 chainId、不记录已用签名)。
一个合法提现签名可被**任何人无限次重放**,反复把金库的币转给 `to` → 抽干。
(注释写着"prevent replay attacks",实际毫无防护。)**严重**。

## H-4 `sendToL1` 执行任意 `target.call` → 签名 = 任意调用原语
`sendToL1` 从签名消息里解码 `(target, value, data)` 并 `target.call{value}(data)`。
这意味着一个 signer 的签名可授权**对任意目标的任意调用**(不限于提现),权限过大;
配合 H-3 的可重放,危害进一步放大。至少应把签名绑定到固定的提现语义。**严重/中**。

## M-1 `TokenFactory.deployToken` 未检查 `create` 返回值
内联汇编 `create` 失败时返回 `addr = 0`,代码不检查就存入映射并 emit,
得到一个指向 0 地址的"已部署"记录。另:在 zkSync 等链上用原始 bytecode `create`
行为不同(已知跨链部署坑)。**中/低**。

## 其它(信息/Gas)
- `DEPOSIT_LIMIT` 可设为 `constant`/`immutable`(它是 `public` 可变但无 setter)。
- `depositTokensToL2` 未遵循 CEI(先外部转账后 emit,虽无直接可利用点)。
- `Deposit` 事件参数未 `indexed`,链下检索不便。
- L2 铸币依赖链下服务监听事件,属**中心化信任假设**(非合约 bug,但应声明)。

## 自评最严重
H-1 / H-2 / H-3 任一都可直接导致资金损失;其中 **H-3 签名重放**危害最干净直接
(一个签名抽干金库),**H-1 任意 from** 最容易被动手。下面给 H-1 和 H-3 各一个 PoC。
