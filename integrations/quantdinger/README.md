# quantbot → QuantDinger 策略移植

`donchian_breakout_v2.py` 是 quantbot `donchian` 策略 + `RiskManager` 风控在
[QuantDinger](https://github.com/brokermr810/QuantDinger) **Strategy API V2** 下的版本
(按 QuantDinger v5.6.1 的 `docs/trading/STRATEGY_DEV_GUIDE_CN.md` 与 `strategy_v2/contract.py` 编写)。

## 使用

1. QuantDinger 策略 IDE → 新建脚本 → 粘贴 `donchian_breakout_v2.py` 全文 → 保存。
2. 点击"验证" (或 `POST /api/strategies/verify`), 确认 `valid: true`、manifest 中
   universe 为 `Crypto:BTC/USDT@spot`、周期 `1d`、`direction_mode=long_only`。
3. 回测时选择日期、资金、手续费和滑点; 参数面板会显示下表参数。
4. 回测满意后创建部署, 先用 `signal` 模式观察, 再考虑 `live`。

想换标的/周期, 直接修改 `initialize` 里的 `g.symbol` 和 `subscribe(frequency=...)`
(QuantDinger 规定标的与周期属于源码, 不能在运行面板覆盖), `get_history` 的周期需同步修改。

## 参数对照

| QuantDinger 参数 | 默认 | quantbot 对应 |
| --- | --- | --- |
| `entry_period` | 20 | `DonchianBreakout.entry` |
| `exit_period` | 10 | `DonchianBreakout.exit` |
| `atr_period` | 14 | `RiskConfig.atr_period` |
| `stop_atr` | 2.0 | `RiskConfig.stop_loss_atr` (0 = 关闭) |
| `trailing` | true | `RiskConfig.trailing_stop` |
| `risk_per_trade` | 0.02 | `RiskConfig.risk_per_trade` (0 = 关闭) |
| `max_position_pct` | 0.95 | `RiskConfig.max_position_pct` (现货不加杠杆, 留 5% 给手续费) |
| `max_drawdown_halt` | 0.30 | `RiskConfig.max_drawdown_halt` (0 = 关闭) |

## 与 quantbot 原策略的差异

| 项目 | quantbot | QuantDinger 版 |
| --- | --- | --- |
| 信号与成交时序 | 收盘信号, 次日开盘成交 | 相同 (平台默认语义) |
| 初始止损 | 入场成交均价 − 2×ATR, 盘中触发 | `stop_loss_pct = 2×ATR/收盘价`, 平台按入场成交价盘中触发 |
| 移动止损 | 收盘价 − 2×ATR 逐日上移, **盘中**触发 | 同样的 ATR 吊灯止损逐日上移, 但在**收盘**检查, 次日开盘离场 |
| 止损后再入场 | 等通道离场信号出现后才允许再入场 | 相同 |
| 仓位 | `min(最大仓位, 风险比例 / 止损比例)` | 相同 |
| 回撤熔断 | 永久停止 | 相同, 状态通过 `PERSIST_RUNTIME_STATE` 跨重启保留 |
| 做空 | 可选 | 未移植 (现货只能做多; 做空需改成 swap 并声明 `direction_mode`) |

为什么不用平台自带的 `trailing_stop_pct`: QuantDinger 的移动止损是"入场后最高价 × (1 − 固定百分比)",
比 quantbot 的 ATR 吊灯止损紧得多。在合成数据上直接套用会让收益从 +5.9% 变成 −10.3%, 所以改为在策略里自己计算 ATR 移动止损。

## 用 QuantDinger 自身引擎验证 (v5.6.1)

用 QuantDinger 后端的 `compile_strategy_v2` 和 `StrategyV2BacktestRunner` 直接运行了本策略
(合成数据, 成交量放大 1000 倍以免触发其成交量上限):

- 编译通过; manifest: `cta`、`Crypto:BTC/USDT@spot`、驱动周期 `1d`、预热 250、`direction_mode=long_only`、不允许杠杆。
- 关闭风控时, 每笔成交日期与 quantbot `donchian` 完全一致 (三组数据共 98 笔)。
- 默认参数下, 每笔成交的日期与原因与 `mock_runtime.py` 完全一致, 收益也一致。
- 计入 0.1% 手续费 + 0.05% 滑点: +11.5% / −9.3% / +13.4% (种子 42 / 7 / 123)。

注意: QuantDinger 回测会按 K 线成交量限制单笔成交, 超出部分顺延到下一根 K 线 (账本状态 `partial`)。
资金量相对标的成交量较大时, 出场可能分两天完成。

## 离线检查 (不依赖 QuantDinger)

`mock_runtime.py` 按其文档实现了本策略用到的最小 API 子集 (次日开盘成交、按比例止损、离场原因),
上面的验证表明它与 QuantDinger 引擎对本策略的结果一致。`tests/test_quantdinger_port.py` 用它检查:

- 源码满足编译器的静态规则 (顶层只有字面量/函数、允许的 import、`initialize` 不读参数、只调用已注册的全局 API、
  `@param` 默认值与代码回退值一致);
- 关闭风控时, 进出场时间与 quantbot `donchian` **逐笔一致**;
- 开启风控时, 止损离场后不会立即再入场, 回撤熔断生效。

合成数据 (`generate_synthetic`, 无手续费) 上默认参数的结果:

| 种子 | QuantDinger 版 (模拟器) | quantbot |
| --- | --- | --- |
| 42 | +13.1%, 最大回撤 11.2% | +5.9%, 最大回撤 10.8% |
| 7 | −7.9%, 最大回撤 13.2% | −10.5%, 最大回撤 12.9% |
| 123 | +15.3%, 最大回撤 9.0% | +11.3%, 最大回撤 9.0% |

模拟器不是 QuantDinger 官方运行时; QuantDinger 升级后请重新在其中验证。
