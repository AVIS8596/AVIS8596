"""quantbot —— 一个轻量的量化交易机器人框架。

模块:
    data        行情数据加载 (CSV / 合成数据 / ccxt / yfinance)
    indicators  技术指标 (SMA/EMA/RSI/MACD/布林带/ATR/唐奇安通道)
    strategies  交易策略与策略注册表
    risk        风险管理 (仓位计算、ATR 止损止盈、移动止损、回撤熔断)
    backtest    逐 K 线事件驱动回测引擎 (含手续费与滑点, 无未来函数)
    metrics     绩效指标 (CAGR、夏普、索提诺、最大回撤、胜率 ...)
    optimize    参数网格搜索 + 样本外验证
    broker      模拟盘 / ccxt 实盘券商接口
    bot         实时 / 模拟交易循环
"""

from .backtest import Backtester, BacktestConfig, BacktestResult
from .risk import RiskConfig, RiskManager
from .strategies import STRATEGIES, Strategy, get_strategy

__all__ = [
    "Backtester",
    "BacktestConfig",
    "BacktestResult",
    "RiskConfig",
    "RiskManager",
    "STRATEGIES",
    "Strategy",
    "get_strategy",
]

__version__ = "0.1.0"
