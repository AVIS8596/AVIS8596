# quantbot —— 量化交易机器人

一个轻量、可测试、无未来函数的 Python 量化交易框架, 覆盖从 **策略研发 → 回测 → 参数优化 → 模拟盘 → 实盘** 的完整流程。
核心只依赖 `numpy` 和 `pandas`。

> ⚠️ 风险提示: 本项目仅用于学习与研究。历史回测不代表未来收益, 实盘交易可能造成资金损失, 请自行承担风险。

## 功能

| 模块 | 说明 |
| --- | --- |
| `data` | CSV (支持中文列名)、合成行情、`ccxt` 交易所 K 线、`yfinance` 股票数据 |
| `indicators` | SMA / EMA / RSI / MACD / 布林带 / ATR / 唐奇安通道 |
| `strategies` | 买入持有、均线交叉 (SMA/EMA)、RSI 均值回归、布林带回归、MACD 动量、唐奇安突破、组合投票; 均可选做空 |
| `risk` | 固定比例 / 按风险定仓、ATR 止损止盈、移动止损、最大回撤熔断、整手交易 |
| `backtest` | 逐 K 线事件驱动引擎: 收盘出信号、次日开盘成交, 含手续费与滑点, 跳空止损按开盘价成交 |
| `metrics` | 总收益、年化、波动率、夏普、索提诺、最大回撤、卡玛、胜率、盈亏比、持仓占比 |
| `optimize` | 网格搜索 (样本内/样本外) 与滚动窗口 (walk-forward) 验证 |
| `broker` | `PaperBroker` 模拟盘 (状态可持久化) / `CcxtBroker` 实盘 (默认 dry-run) |
| `bot` | 实时交易循环, 与回测使用同一套决策与风控规则 |

## 安装

```bash
pip install -e ".[dev]"        # 基础 + 绘图 + 测试
pip install -e ".[live]"       # 需要交易所数据/实盘时
```

## 快速开始

```bash
# 列出策略及默认参数
python -m quantbot list

# 用默认参数对比所有策略 (合成数据)
python -m quantbot compare

# 回测: 唐奇安突破 + 2 倍 ATR 移动止损 + 每笔风险 2% + 30% 回撤熔断, 并输出图表
python -m quantbot backtest -s donchian --stop-atr 2 --trailing --risk-per-trade 0.02 --max-dd 0.3 --plot result.png

# 自定义策略参数
python -m quantbot backtest -s sma_cross -p fast=10 -p slow=60 -p allow_short=true --trades

# 使用自己的数据 (列: date/open/high/low/close/volume 或 日期/开盘/最高/最低/收盘/成交量)
python -m quantbot backtest -s rsi_reversion -d data/600519.csv

# 交易所数据 (需 ccxt)
python -m quantbot backtest -s ema_cross -d ccxt:BTC/USDT --timeframe 4h --limit 1000
```

### 参数优化

```bash
# 70% 训练 / 30% 测试, 按训练集夏普排序, 同时显示样本外表现
python -m quantbot optimize -s sma_cross -g "fast=[5,10,20]" -g "slow=[50,100,200]"

# 滚动窗口验证 (更能反映真实表现)
python -m quantbot optimize -s donchian -g "entry=[20,55]" -g "exit=[10,20]" --walk-forward 4
```

注意: 关注 **样本外** 指标, 训练集表现最好的参数往往是过拟合的。

### 运行交易机器人

```bash
# 模拟盘: 用历史数据逐根回放, 驱动真实的机器人循环
python -m quantbot -v trade -s sma_cross --stop-atr 3 --trailing --state paper.state.json

# 实盘 dry-run: 拉取交易所实时 K 线, 只打印订单不下单
python -m quantbot -v trade --live -d ccxt:BTC/USDT --timeframe 1h -s ema_cross --stop-atr 2 --interval 60

# 真实下单 (危险!): 先设置 API 密钥环境变量
export QUANTBOT_API_KEY=...  QUANTBOT_API_SECRET=...
python -m quantbot -v trade --live --confirm-live -d ccxt:BTC/USDT --timeframe 1h -s ema_cross --stop-atr 2 --max-dd 0.2
```

实盘建议: 先在交易所测试网 (`CcxtBroker(sandbox=True)`) 运行, 用小资金验证, 并始终开启止损与回撤熔断。
当前 `CcxtBroker` 面向现货 (仅做多); 做空需接入合约接口。

## 编写自定义策略

继承 `Strategy`, 实现 `generate_signals(df)`, 返回与 `df` 同索引、取值在 `[-1, 1]` 的目标仓位:

```python
from quantbot import Strategy, Backtester, RiskManager, RiskConfig
from quantbot import indicators as ind

class MyStrategy(Strategy):
    name = "my"
    default_params = {"period": 50}

    def generate_signals(self, df):
        above = df["close"] > ind.sma(df["close"], self.params["period"])
        return above.astype(float)

res = Backtester(MyStrategy(period=30), RiskManager(RiskConfig(stop_loss_atr=2))).run(df)
print(res.summary())
res.plot("my.png")
```

完整示例见 [`examples/custom_strategy.py`](examples/custom_strategy.py)。在 `strategies.STRATEGIES` 中注册后即可在命令行使用。

## 回测规则

1. 第 *t* 根 K 线收盘后计算信号 (只用截至 *t* 的数据);
2. 第 *t+1* 根 K 线 **开盘价** 成交, 买入价 × (1+滑点), 卖出价 × (1−滑点), 另收手续费;
3. 盘中检查止损/止盈: 若开盘已跳空越过止损价, 则按开盘价成交; 同一根 K 线同时触及止损与止盈时, 保守地按止损处理;
4. 止损离场后, 需等待信号先反转 (或归零) 才会再次同向入场;
5. 收盘更新移动止损、记录权益, 回撤超过阈值则下一根开盘平仓并停止交易;
6. 数据结束时按最后收盘价平仓以统计完整交易。

测试 (`tests/`) 覆盖了无未来函数 (截断数据不改变历史信号)、成交价格时序、手续费、做空、止损、仓位计算、熔断、资金守恒以及模拟盘机器人。

```bash
pytest -q
```

## 项目结构

```
quantbot/
  data.py  indicators.py  strategies.py  risk.py
  backtest.py  metrics.py  optimize.py
  broker.py  bot.py  cli.py
tests/test_quantbot.py
examples/custom_strategy.py
```
