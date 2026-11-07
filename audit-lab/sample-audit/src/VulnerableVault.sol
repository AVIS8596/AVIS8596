// SPDX-License-Identifier: MIT
pragma solidity ^0.8.13;

/// @title VulnerableVault
/// @notice 故意写的有漏洞合约，用来验证 Slither 能否检出问题。请勿部署到生产环境。
contract VulnerableVault {
    mapping(address => uint256) public balances;

    function deposit() external payable {
        balances[msg.sender] += msg.value;
    }

    /// @dev 经典重入漏洞：先转账、后更新余额 (违反 checks-effects-interactions)
    function withdraw() external {
        uint256 amount = balances[msg.sender];
        require(amount > 0, "no balance");

        // 外部调用在状态更新之前 —— 可被重入
        (bool ok, ) = msg.sender.call{value: amount}("");
        require(ok, "transfer failed");

        balances[msg.sender] = 0;
    }

    // 未使用的、带 tx.origin 鉴权的函数：又一个 Slither 会标记的反模式
    address public owner;

    function setOwner(address newOwner) external {
        require(tx.origin == owner, "not owner"); // tx.origin 鉴权：危险
        owner = newOwner;
    }
}
