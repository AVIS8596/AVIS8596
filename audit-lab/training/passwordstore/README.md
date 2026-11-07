# 训练靶场 #2：PasswordStore(CodeHawks First Flight)

> 训练用。换一类漏洞:**访问控制 + "链上数据不是私密的"**,和 Puppy Raffle 完全不同。
> 代码:`Cyfrin/3-passwordstore-audit` · `src/PasswordStore.sol`(solc 0.8.18)。

## 发现(2 个 High,对照官方答案全中)

| 级别 | 漏洞 | PoC | 结果 |
|------|------|-----|------|
| High | **`private` 存储不是私密的** —— `s_password` 虽标 `private`,任何人都能用 `eth_getStorageAt(slot 1)` 读出明文 | `test_H1_privatePasswordIsReadableFromStorage` | 直接从槽里还原出 `mySuperSecretPwd` |
| High | **`setPassword` 缺访问控制** —— NatSpec 写"only owner",函数体却没有任何 owner 校验,任何人可改密码 | `test_H2_anyoneCanSetPassword` | 攻击者成功覆盖密码 |

## 核心教训

1. **`private` ≠ 机密**。Solidity 的 `private` 只限制**合约间**的读取,链上存储对所有人透明;
   `eth_getStorageAt` / `vm.load` 直接读槽即可。**任何需要保密的东西都不能明文上链**
   (要么别上链,要么存承诺/哈希/密文)。
2. **注释不是访问控制**。NatSpec 说"only owner"毫无约束力;权限必须在**函数体里用
   `require`/`modifier` 真正校验**。审计时永远以代码为准,不信注释。

> 修复:H-1 不要把明文密码上链(本质上这个合约的设计目标就不成立);
> H-2 给 `setPassword` 加 `if (msg.sender != s_owner) revert PasswordStore__NotOwner();`。

## 工具 vs 人工

- Slither 对 H-2(缺访问控制 / setter 无保护)有一定提示能力;
- 但 H-1 "把秘密明文上链"是**设计/语义层面的错误**,静态工具不会告诉你"这个需求本身在公链上不成立"——这要靠理解区块链模型的人来判断。

## 复现

```bash
source audit-lab/env.sh
git clone https://github.com/Cyfrin/3-passwordstore-audit passwordstore && cd passwordstore
git submodule update --init --recursive
# foundry.toml 里设 solc_version="0.8.18"、offline=true、remappings=['forge-std/=lib/forge-std/src/']
cp ../audit-lab/training/passwordstore/AuditPoC.t.sol test/
FOUNDRY_OFFLINE=true forge test --match-path test/AuditPoC.t.sol -vv
```
