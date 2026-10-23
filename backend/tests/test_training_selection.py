from datetime import date, timedelta
from types import SimpleNamespace

import duckdb
import polars as pl
import pytest

from app.custom.training import CreateSession
from app.services.training_selection import BOARDS, TYPE_IDS, board, catalog, topic_scan


@pytest.fixture
def topic_market():
    dates = [(date(2023, 1, 1) + timedelta(days=i)).isoformat() for i in range(330)]
    db = duckdb.connect()
    rows = []
    for i, day in enumerate(dates):
        rows.extend([('600001.SH', day, 10 + i * .5, 400 if i == 300 else 100, 2 if i == 300 else 0, 0),
                     ('000002.SZ', day, 500 - i * .5, 100, 0, 2 if i == 300 else 0),
                     ('300001.SZ', day, 50, 100, 0, 0)])
    frame = pl.DataFrame(rows, schema=['symbol', 'date', 'close', 'volume', 'consecutive_limit_ups', 'consecutive_limit_downs'], orient='row')
    db.register('fixture_data', frame.to_arrow())
    db.execute('CREATE TABLE kline_enriched AS SELECT symbol, CAST(date AS DATE) AS date, close, volume, '
               'consecutive_limit_ups, consecutive_limit_downs FROM fixture_data')
    names = pl.DataFrame({'symbol': ['600001.SH', '000002.SZ', '300001.SZ'], 'name': ['向上样本', '向下样本', 'ST样本']})
    repo = SimpleNamespace(execute_all=lambda q, p: db.execute(q, p).fetchall(),
                           get_instruments_asset=lambda kind: names)
    yield repo, dates, db
    db.close()


@pytest.mark.parametrize('key,expected', [
    ('random', {'600001.SH', '000002.SZ', '300001.SZ'}),
    ('history_high', {'600001.SH', '300001.SZ'}), ('history_low', {'000002.SZ', '300001.SZ'}),
    ('year_high', {'600001.SH', '300001.SZ'}), ('year_low', {'000002.SZ', '300001.SZ'}),
    ('high_200', {'600001.SH', '300001.SZ'}), ('low_200', {'000002.SZ', '300001.SZ'}),
    ('top_15', {'600001.SH'}), ('top_30', {'600001.SH'}),
    ('bottom_15', {'000002.SZ'}), ('bottom_30', {'000002.SZ'}),
    ('limit_up', {'600001.SH'}), ('consecutive_up', {'600001.SH'}),
    ('limit_down', {'000002.SZ'}), ('consecutive_down', {'000002.SZ'}),
    ('volume_price', {'600001.SH'}), ('uptrend', {'600001.SH'}), ('downtrend', {'000002.SZ'}),
])
def test_topic_predicates_and_future_invariance(topic_market, key, expected):
    repo, dates, db = topic_market
    result = topic_scan(repo, key, dates[300], dates, BOARDS, False)
    assert {r[0] for r in result} == expected
    db.execute('UPDATE kline_enriched SET close=999999, volume=999999, '
               'consecutive_limit_ups=99, consecutive_limit_downs=99 WHERE date > ?', [dates[300]])
    assert topic_scan(repo, key, dates[300], dates, BOARDS, False) == result


def test_board_st_and_insufficient_history(topic_market):
    repo, dates, _ = topic_market
    assert topic_scan(repo, 'random', dates[300], dates, {'growth'}, True) == []
    assert topic_scan(repo, 'random', dates[300], dates, {'sz_main'}, True) == [('000002.SZ', '向下样本')]
    assert topic_scan(repo, 'year_high', dates[200], dates, BOARDS, False) == []
    assert topic_scan(repo, 'random', dates[118], dates, BOARDS, False) == []
    assert board('920001.BJ') == 'beijing'
    assert board('688001.SH') == 'star'


def test_rank_fifty_after_board_filter(topic_market):
    repo, dates, db = topic_market
    extra = [(f'600{i+100:03d}.SH', d, 10 + j * (i+1)/100, 100, 0, 0)
             for i in range(60) for j, d in enumerate(dates)]
    frame = pl.DataFrame(extra, schema=['symbol', 'date', 'close', 'volume', 'consecutive_limit_ups', 'consecutive_limit_downs'], orient='row')
    db.register('rank_data', frame.to_arrow())
    db.execute('INSERT INTO kline_enriched SELECT symbol, CAST(date AS DATE), close, volume, '
               'consecutive_limit_ups, consecutive_limit_downs FROM rank_data')
    result = topic_scan(repo, 'top_15', dates[300], dates, {'sh_main'}, False)
    assert len(result) == 50 and result[0][0] == '600159.SH'
    assert '600100.SH' not in {r[0] for r in result}


def test_api_selection_contract():
    assert len(catalog()) == len(TYPE_IDS) == 19
    assert CreateSession(request_id='random', selection_type='random', candidate_count=20).strategy_ids == []
    for changes in [dict(selection_type='unknown'), dict(candidate_count=21), dict(boards=[]),
                    dict(scan_interval_min=10, scan_interval_max=2),
                    dict(history_start='2025-02-01', history_end='2025-01-01')]:
        with pytest.raises(ValueError):
            CreateSession(request_id='bad', selection_type=changes.pop('selection_type', 'random'), **changes)
    with pytest.raises(ValueError):
        CreateSession(request_id='no-strategy')
