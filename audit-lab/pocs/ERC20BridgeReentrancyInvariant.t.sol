// SPDX-License-Identifier: UNLICENSED
pragma solidity ^0.8.4;

import "forge-std/Test.sol";
import "./util/TestUtil.sol";
import "../../src/bridge/ERC20Bridge.sol";
import "@openzeppelin/contracts/token/ERC20/presets/ERC20PresetMinterPauser.sol";
import {CallNotAllowed, NotOutbox, CallTargetNotAllowed} from "../../src/libraries/Error.sol";

/// @title ERC20Bridge._executeLowLevelCall 余额守卫的重入安全性测试
/// @notice 针对审计报告里 reentrancy-balance 发现，验证性质：
///   "经由 outbox 的一次 executeCall，Bridge 释放的 native token 数量不得
///    超过显式声明的 value —— 即 data 外呼不能被用来额外抽走托管资产。"
///   守卫逻辑：在 `to.call(data)` 前后读 Bridge 的 native token 余额，
///   若 after < before 则 revert CallNotAllowed()。

// ---------------------------------------------------------------------------
// 攻击者合约
// ---------------------------------------------------------------------------

/// 恶意合约：被 Bridge 以 to.call(data) 调到 hook() 时尝试重入 executeCall
/// 再抽一笔。mode 控制行为；asOutbox 决定它自己是否被注册为 outbox。
contract ReentrantReceiver {
    IBridge public immutable bridge;
    address public immutable token;

    uint256 public extra; // 重入时试图额外抽取的数量

    constructor(IBridge _bridge, address _token) {
        bridge = _bridge;
        token = _token;
    }

    function setExtra(uint256 _extra) external {
        extra = _extra;
    }

    /// Bridge 在 data 窗口内会调用这里
    function hook() external returns (bool) {
        if (extra > 0) {
            // 重入：试图在 data 窗口内再抽走 extra 个 native token。
            // - 若本合约未注册为 outbox -> revert NotOutbox（访问控制拦截）
            // - 若已注册为 outbox      -> 内层转账成功、余额下降 ->
            //                            外层守卫 after<before -> revert CallNotAllowed
            bridge.executeCall(address(this), extra, "");
        }
        return true;
    }
}

// ---------------------------------------------------------------------------
// 单元级 PoC
// ---------------------------------------------------------------------------

contract ERC20BridgeReentrancyTest is Test {
    ERC20Bridge public bridge;
    IERC20 public nativeToken;

    address public rollup = address(1000);
    uint256 public constant ESCROW = 1_000_000 ether;

    function setUp() public {
        nativeToken = new ERC20PresetMinterPauser("Appchain Token", "APP");
        bridge = ERC20Bridge(TestUtil.deployProxy(address(new ERC20Bridge())));
        bridge.initialize(IOwnable(rollup), address(nativeToken));
        // 给 Bridge 预置一笔托管资产
        ERC20PresetMinterPauser(address(nativeToken)).mint(address(bridge), ESCROW);
    }

    function _allowOutbox(
        address o
    ) internal {
        vm.prank(rollup);
        bridge.setOutbox(o, true);
    }

    /// 守卫有效：恶意 outbox 想在 data 窗口内重入多抽一笔 -> 整笔 revert CallNotAllowed
    function test_guardBlocks_reentrantOutboxExtraWithdraw() public {
        ReentrantReceiver attacker = new ReentrantReceiver(IBridge(address(bridge)), address(nativeToken));
        // 攻击者自身被注册为 outbox（模拟 outbox 被攻陷 / 恶意 outbox）
        _allowOutbox(address(attacker));
        attacker.setExtra(500_000 ether); // 试图额外抽走

        bytes memory data = abi.encodeWithSelector(ReentrantReceiver.hook.selector);

        vm.prank(address(attacker));
        vm.expectRevert(CallNotAllowed.selector);
        bridge.executeCall(address(attacker), 1 ether, data);

        // 整笔回滚，托管资产原封不动
        assertEq(nativeToken.balanceOf(address(bridge)), ESCROW, "escrow must be intact");
    }

    /// 访问控制有效：非 outbox 的 to 无法重入 executeCall，只能拿到声明的 value
    function test_accessControl_nonOutboxCannotReenter() public {
        ReentrantReceiver attacker = new ReentrantReceiver(IBridge(address(bridge)), address(nativeToken));
        address outbox = address(1002);
        _allowOutbox(outbox); // 只有 outbox 被授权，attacker 不是
        attacker.setExtra(500_000 ether); // 会触发重入，但应因 NotOutbox 失败

        bytes memory data = abi.encodeWithSelector(ReentrantReceiver.hook.selector);
        uint256 declared = 10 ether;

        vm.prank(outbox);
        // 外呼里的重入会 revert(NotOutbox)，但 to.call 吞掉失败，返回 success=false；
        // 外层 executeCall 不 revert，只转出声明的 declared。
        bridge.executeCall(address(attacker), declared, data);

        // Bridge 仅减少了声明的 declared，没有被额外抽走
        assertEq(
            nativeToken.balanceOf(address(bridge)), ESCROW - declared, "only declared value leaves"
        );
        assertEq(nativeToken.balanceOf(address(attacker)), declared, "attacker got only declared");
    }

    /// to == nativeToken 在入口被拒（守卫之外的另一道防线）
    function test_cannotCallNativeToken() public {
        address outbox = address(1002);
        _allowOutbox(outbox);
        vm.prank(outbox);
        vm.expectRevert(abi.encodeWithSelector(CallTargetNotAllowed.selector, address(nativeToken)));
        bridge.executeCall(address(nativeToken), 0, abi.encodeWithSignature("transfer(address,uint256)", address(1), 1));
    }
}

// ---------------------------------------------------------------------------
// 不变量测试（fuzz 序列）
// ---------------------------------------------------------------------------

/// Handler：被注册为 outbox，由 fuzzer 反复调用，驱动对抗性的 executeCall 序列。
contract BridgeHandler is Test {
    ERC20Bridge public immutable bridge;
    IERC20 public immutable token;
    ReentrantReceiver public immutable attacker;

    uint256 public declaredOut; // 累计"声明释放"的 value（仅统计未回滚的调用）

    constructor(ERC20Bridge _bridge, IERC20 _token, ReentrantReceiver _attacker) {
        bridge = _bridge;
        token = _token;
        attacker = _attacker;
    }

    /// fuzzer 入口：对 attacker 发起一次 executeCall；attacker 可能重入多抽。
    function execute(uint256 value, uint256 extra, bool withData) external {
        uint256 bal = token.balanceOf(address(bridge));
        if (bal == 0) return;
        value = bound(value, 0, bal);
        extra = bound(extra, 0, bal);
        attacker.setExtra(extra);

        bytes memory data = withData ? abi.encodeWithSelector(ReentrantReceiver.hook.selector) : bytes("");

        // 守卫触发(CallNotAllowed)时整笔 revert；用 try/catch 吸收，只在成功时记账。
        try bridge.executeCall(address(attacker), value, data) {
            declaredOut += value;
        } catch {}
    }
}

contract ERC20BridgeInvariantTest is Test {
    ERC20Bridge public bridge;
    IERC20 public nativeToken;
    ReentrantReceiver public attacker;
    BridgeHandler public handler;

    address public rollup = address(1000);
    uint256 public constant ESCROW = 1_000_000 ether;

    function setUp() public {
        nativeToken = new ERC20PresetMinterPauser("Appchain Token", "APP");
        bridge = ERC20Bridge(TestUtil.deployProxy(address(new ERC20Bridge())));
        bridge.initialize(IOwnable(rollup), address(nativeToken));
        ERC20PresetMinterPauser(address(nativeToken)).mint(address(bridge), ESCROW);

        attacker = new ReentrantReceiver(IBridge(address(bridge)), address(nativeToken));
        handler = new BridgeHandler(bridge, nativeToken, attacker);

        // handler 与 attacker 都注册为 outbox：这样 attacker 的重入会真正走到
        // 守卫路径（内层转账成功 -> 余额下降 -> 外层守卫拦截），让不变量真正考验守卫。
        vm.startPrank(rollup);
        bridge.setOutbox(address(handler), true);
        bridge.setOutbox(address(attacker), true);
        vm.stopPrank();

        targetContract(address(handler));
    }

    /// 核心不变量：Bridge 的 native token 净流出 == 累计声明的 value。
    /// 若重入能额外抽走托管资产，则 净流出 > declaredOut，断言失败。
    function invariant_outflowNeverExceedsDeclared() public {
        uint256 outflow = ESCROW - nativeToken.balanceOf(address(bridge));
        assertEq(outflow, handler.declaredOut(), "bridge released more than declared -> escrow drained");
    }
}
