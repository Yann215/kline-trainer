"""Practice topic selectors. Their predicates use known bars, never outcomes."""
# ruff: noqa: RUF001 -- Chinese product copy.
from __future__ import annotations

from app.price_limits import is_risk_warning_name

SELECTION_TYPES = [
    ('random', '随机行情', '基础', '从当日有行情且至少120根历史记录的股票中随机抽取。'),
    ('strategy', '当前策略', '基础', '沿用已保存的策略参数，按历史时点取候选并集。'),
    ('history_high', '历史新高（现有数据）', '新高与强势', '收盘价达到截至当天本地可用历史的最高收盘价。'),
    ('year_high', '1年新高', '新高与强势', '至少252根日线，收盘价达到近252根最高收盘价。'),
    ('high_200', '200日新高', '新高与强势', '至少200根日线，收盘价达到近200根最高收盘价。'),
    ('top_30', '30日涨幅前50', '新高与强势', '按30个个股交易记录前至今的收盘涨幅降序取前50。'),
    ('top_15', '15日涨幅前50', '新高与强势', '按15个个股交易记录前至今的收盘涨幅降序取前50。'),
    ('limit_up', '涨停', '量价与趋势', '当天封住涨停，复用TSP已落盘的连续涨停状态。'),
    ('consecutive_up', '连板', '量价与趋势', '截至当天连续涨停至少2个个股交易日。'),
    ('volume_price', '量升价涨', '量价与趋势', '收盘高于前收盘，成交量至少为前5日平均量的1.2倍。'),
    ('uptrend', '上升趋势', '量价与趋势', '收盘>MA5>MA10>MA20>MA60，且MA20高于5日前。'),
    ('history_low', '历史新低（现有数据）', '新低与弱势', '收盘价达到截至当天本地可用历史的最低收盘价。'),
    ('year_low', '1年新低', '新低与弱势', '至少252根日线，收盘价达到近252根最低收盘价。'),
    ('low_200', '200日新低', '新低与弱势', '至少200根日线，收盘价达到近200根最低收盘价。'),
    ('bottom_30', '30日跌幅前50', '新低与弱势', '按30日收盘收益升序取前50，收益须小于0。'),
    ('bottom_15', '15日跌幅前50', '新低与弱势', '按15日收盘收益升序取前50，收益须小于0。'),
    ('downtrend', '下降趋势', '新低与弱势', '收盘<MA5<MA10<MA20<MA60，且MA20低于5日前。'),
    ('limit_down', '跌停', '新低与弱势', '当天封住跌停，复用TSP已落盘的连续跌停状态。'),
    ('consecutive_down', '连续跌停', '新低与弱势', '截至当天连续跌停至少2个个股交易日。'),
]
TYPE_IDS = {row[0] for row in SELECTION_TYPES}
BOARDS = {'sh_main', 'sz_main', 'growth', 'star', 'beijing'}


def catalog():
    return [dict(id=key, name=name, group=group, description=description)
            for key, name, group, description in SELECTION_TYPES]


def selection_name(key):
    return next((r[1] for r in SELECTION_TYPES if r[0] == key), key)


def board(symbol):
    if symbol.endswith('.BJ'):
        return 'beijing'
    if symbol.startswith(('300', '301')) and symbol.endswith('.SZ'):
        return 'growth'
    if symbol.startswith(('688', '689')) and symbol.endswith('.SH'):
        return 'star'
    if symbol.startswith('6') and symbol.endswith('.SH'):
        return 'sh_main'
    if symbol.startswith(('000', '001', '002', '003')) and symbol.endswith('.SZ'):
        return 'sz_main'
    return ''


def filter_universe(candidates, boards, exclude_st):
    return [(symbol, name) for symbol, name in candidates
            if board(symbol) in boards and (not exclude_st or not is_risk_warning_name(name))]


def topic_scan(repo, key, as_of, calendar, boards, exclude_st):
    """Small SQL projection through repository's locked query contract.

    Raw enriched contains the stored consecutive-limit states. Technical windows
    are ordinary trailing close/volume aggregates, matching the topic definitions;
    they do not use today's finance, capitalization, ranking or industry state.
    """
    known_dates = [d for d in calendar if d <= as_of]
    start = '1900-01-01' if key in {'history_high', 'history_low'} else known_dates[max(0, len(known_dates) - 360)]
    conditions = {
        'random': 'true',
        'history_high': 'close >= hi_all', 'history_low': 'close <= lo_all',
        'year_high': 'n >= 252 AND close >= hi252', 'year_low': 'n >= 252 AND close <= lo252',
        'high_200': 'n >= 200 AND close >= hi200', 'low_200': 'n >= 200 AND close <= lo200',
        'top_30': 'ret30 > 0', 'bottom_30': 'ret30 < 0',
        'top_15': 'ret15 > 0', 'bottom_15': 'ret15 < 0',
        'limit_up': 'consecutive_limit_ups >= 1', 'consecutive_up': 'consecutive_limit_ups >= 2',
        'limit_down': 'consecutive_limit_downs >= 1', 'consecutive_down': 'consecutive_limit_downs >= 2',
        'volume_price': 'close > previous AND volume >= v5 * 1.2 AND v5 > 0',
        'uptrend': 'close > m5 AND m5 > m10 AND m10 > m20 AND m20 > m60 AND m20 > old_m20',
        'downtrend': 'close < m5 AND m5 < m10 AND m10 < m20 AND m20 < m60 AND m20 < old_m20',
    }
    if key not in conditions:
        raise ValueError('不支持的选股类型')
    # Only fixed allowlisted expressions enter SQL; user dates are parameters.
    sql = """
        WITH bars AS (
            SELECT symbol, date, close, volume, consecutive_limit_ups, consecutive_limit_downs
            FROM kline_enriched WHERE date >= ? AND date <= ?
        ), stats AS (
            SELECT *, count(*) OVER w AS n, lag(close) OVER w AS previous,
                close / nullif(lag(close,15) OVER w,0)-1 AS ret15,
                close / nullif(lag(close,30) OVER w,0)-1 AS ret30,
                max(close) OVER w AS hi_all, min(close) OVER w AS lo_all,
                max(close) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS hi200,
                min(close) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 199 PRECEDING AND CURRENT ROW) AS lo200,
                max(close) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW) AS hi252,
                min(close) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 251 PRECEDING AND CURRENT ROW) AS lo252,
                avg(volume) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 5 PRECEDING AND 1 PRECEDING) AS v5,
                avg(close) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 4 PRECEDING AND CURRENT ROW) AS m5,
                avg(close) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 9 PRECEDING AND CURRENT ROW) AS m10,
                avg(close) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 19 PRECEDING AND CURRENT ROW) AS m20,
                avg(close) OVER (PARTITION BY symbol ORDER BY date ROWS BETWEEN 59 PRECEDING AND CURRENT ROW) AS m60
            FROM bars WINDOW w AS (PARTITION BY symbol ORDER BY date ROWS BETWEEN UNBOUNDED PRECEDING AND CURRENT ROW)
        ), ready AS (
            SELECT *, lag(m20,5) OVER (PARTITION BY symbol ORDER BY date) AS old_m20 FROM stats
        ) SELECT symbol, ret15, ret30 FROM ready WHERE date = ? AND n >= 120
            AND close > 0 AND volume > 0 AND (CONDITION)
    """.replace('CONDITION', conditions[key])
    params = [start, as_of, as_of]
    if key in {'random', 'history_high', 'history_low'}:
        extreme = {'random': 'true', 'history_high': 'arg_max(close,date) >= max(close)',
                   'history_low': 'arg_max(close,date) <= min(close)'}[key]
        sql = """SELECT symbol, NULL, NULL FROM kline_enriched WHERE date >= ? AND date <= ?
                 GROUP BY symbol HAVING max(date) = ? AND count(*) >= 120
                 AND arg_max(close,date) > 0 AND arg_max(volume,date) > 0 AND (CONDITION)
              """.replace('CONDITION', extreme)
    rows = repo.execute_all(sql, params)
    instruments = repo.get_instruments_asset('stock')
    names = dict(instruments.select('symbol', 'name').unique('symbol').iter_rows()) if not instruments.is_empty() else {}
    rows = [r for r in rows if board(r[0]) in boards and
            (not exclude_st or not is_risk_warning_name(names.get(r[0], '')))]
    if key.startswith(('top_', 'bottom_')):
        index = 1 if key.endswith('_15') else 2
        rows = sorted(rows, key=lambda r: ((-r[index] if key.startswith('top_') else r[index]), r[0]))[:50]
    else:
        rows = sorted(rows, key=lambda r: r[0])
    return [(r[0], names.get(r[0], '')) for r in rows]
