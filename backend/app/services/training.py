"""Blind historical practice. Only sanitized known bars leave this service."""
# ruff: noqa: RUF001 -- Chinese product copy uses Chinese punctuation.
from __future__ import annotations

import copy
import hashlib
import json
import math
import random
import sqlite3
import uuid
from collections import Counter
from contextlib import contextmanager
from datetime import date, timedelta
from pathlib import Path

import numpy as np
import polars as pl

from app.backtest.engine import MatcherConfig
from app.indicators.pipeline import compute_indicators
from app.market_time import cn_today
from app.price_limits import is_risk_warning_name, numpy_limit_price, price_limit_pct
from app.services.training_selection import (
    BOARDS,
    TYPE_IDS,
    filter_universe,
    selection_name,
    topic_scan,
)

PRICE_FIELDS = ('open', 'high', 'low', 'close', 'ma5', 'ma10', 'ma20', 'ma60',
                'ma125', 'macd_dif', 'macd_dea', 'macd_hist', 'boll_upper', 'boll_lower')
OSC_FIELDS = ('kdj_k', 'kdj_d', 'kdj_j', 'rsi_6', 'rsi_14', 'rsi_24')
DISPLAY_FIELDS = (*PRICE_FIELDS, *OSC_FIELDS, 'volume')
INDICATORS = {'vol', 'macd', 'kdj', 'rsi', 'boll'}


class TrainingError(ValueError):
    def __init__(self, message: str, status: int = 400):
        super().__init__(message)
        self.status = status


def _dump(value) -> str:
    return json.dumps(value, ensure_ascii=False, separators=(',', ':'), allow_nan=False)


def _hash(value) -> str:
    return hashlib.sha256(_dump(value).encode()).hexdigest()


def _number(value) -> float | None:
    try:
        result = float(value)
    except (ValueError, TypeError):
        return None
    return result if math.isfinite(result) else None


class TrainingStore:
    """SQLite transactions serialize revisions across threads and processes."""

    def __init__(self, data_dir: Path):
        root = Path(data_dir) / 'user_data' / 'training'
        root.mkdir(parents=True, exist_ok=True)
        self.path = root / 'sessions.sqlite3'
        with self.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS sessions '
                       '(id TEXT PRIMARY KEY, create_key TEXT UNIQUE, payload TEXT NOT NULL)')
            db.execute('CREATE TABLE IF NOT EXISTS commands '
                       '(session_id TEXT, request_id TEXT, PRIMARY KEY(session_id, request_id))')

    @contextmanager
    def connect(self, write=True):
        db = sqlite3.connect(self.path, timeout=30)
        try:
            if write:
                db.execute('BEGIN IMMEDIATE')
            yield db
            db.commit()
        except sqlite3.OperationalError as exc:
            db.rollback()
            if getattr(exc, 'sqlite_errorcode', 0) & 0xff in {sqlite3.SQLITE_BUSY, sqlite3.SQLITE_LOCKED}:
                raise TrainingError('训练记录正在被其他操作占用，本次操作未写入，请稍后重试', 409) from exc
            raise
        except BaseException:
            db.rollback()
            raise
        finally:
            db.close()

    @staticmethod
    def read(db, session_id):
        row = db.execute('SELECT payload FROM sessions WHERE id=?', (session_id,)).fetchone()
        if row is None:
            raise TrainingError('训练记录不存在', 404)
        return json.loads(row[0])

    @staticmethod
    def read_step(db, session_id):
        # Ordinary advances need chart rows and event labels, not frozen evidence.
        # Reuse the tiny alias selection. Correlating its JSON extraction with
        # each asset reparses the large payload once per old candidate.
        row = db.execute(
            "WITH source AS (SELECT payload,json_extract(payload,'$.active') active, "
            "json_extract(payload,'$.position.alias') held FROM sessions WHERE id=? "
            "AND json_extract(payload, '$.finished')=0 "
            "AND json_extract(payload, '$.pending') IS NULL "
            "AND (json_extract(payload, '$.auto_scan')=0 OR "
            "json_extract(payload, '$.next_scan')>json_extract(payload, '$.cursor')+1)) "
            "SELECT json_remove(payload, '$.events', '$.cached_index', '$.assets'), "
            "(SELECT json_group_array(json_remove(value, '$.snapshot')) "
            "FROM json_each(payload, '$.events')), "
            "(SELECT json_group_object(key,json(value)) FROM json_each(payload,'$.assets') "
            "WHERE key IN (SELECT value FROM source,json_each(source.active)) "
            "OR key IN (SELECT held FROM source)) FROM source",
            (session_id,)).fetchone()
        if row is None:
            return None
        session = json.loads(row[0])
        session['events'] = json.loads(row[1])
        session['assets'] = json.loads(row[2])
        return session

    @staticmethod
    def write_step(db, session):
        # Keep full historical snapshots/cache unchanged. No schema migration/cache.
        db.execute("UPDATE sessions SET payload=json_set(payload, '$.cursor', ?, "
                   "'$.revision', ?, '$.equity', json(?)) WHERE id=?",
                   (session['cursor'], session['revision'], _dump(session['equity']), session['id']))

    def load(self, session_id):
        with self.connect(write=False) as db:
            return self.read(db, session_id)

    def list(self):
        with self.connect(write=False) as db:
            rows = db.execute('SELECT payload FROM sessions ORDER BY rowid DESC').fetchall()
        # Large historical JSON must be decoded after the SQLite cursor/connection close.
        return [json.loads(row[0]) for row in rows]

    def list_for_display(self, summary=False):
        """Project read-only metadata without decoding frozen charts/evidence."""
        base = "json_remove(payload, '$.assets', '$.events', '$.cached_index', '$.fills', '$.analyses')"
        if summary:
            detail = ("(SELECT json_group_array(json_object('action','buy','tags',json_extract(value,'$.tags'))) "
                      "FROM json_each(payload,'$.events') WHERE json_extract(value,'$.action')='buy')")
            where = "WHERE json_extract(payload,'$.finished')=1 AND coalesce(json_extract(payload,'$.verification'),0)=0"
        else:
            # Mark list returns from the actual frozen holding, including its
            # legacy entry basis. A saved equity point can lag an old record.
            detail = ("(SELECT json_object(key,json(value)) FROM json_each(payload,'$.assets') "
                      "WHERE key=json_extract(payload,'$.position.alias'))")
            where = ''
        with self.connect(write=False) as db:
            rows = db.execute(f'SELECT {base},{detail} FROM sessions {where} ORDER BY rowid DESC').fetchall()
        result = []
        for payload, detail in rows:
            session = json.loads(payload)
            session['events' if summary else 'assets'] = json.loads(detail) if detail else ([] if summary else {})
            result.append(session)
        return result

    def replace_for_test(self, session):
        # Private state fixture utility; never exposed as an API.
        with self.connect() as db:
            db.execute('UPDATE sessions SET payload=? WHERE id=?', (_dump(session), session['id']))


class TrainingMarket:
    """Composition of the existing repository and strategy execution contract."""

    def __init__(self, repo, engine):
        self.repo, self.engine = repo, engine
        self._calendar_snapshot = None

    def generation(self):
        return self.repo.get_matrix_data_generation('stock')

    def calendar(self):
        # Small immutable calendar snapshot, invalidated by data generation/day.
        today = cn_today().isoformat()
        key = (self.generation(), today)
        cached = self._calendar_snapshot
        if cached is not None and cached[0] == key:
            return list(cached[1])
        indexed = self.repo.read_enriched_calendar() if hasattr(self.repo, 'read_enriched_calendar') else None
        rows = [(day,) for day in indexed] if indexed is not None else self.repo.execute_all(
            'SELECT DISTINCT date FROM kline_enriched ORDER BY date')
        days = tuple(str(row[0])[:10] for row in rows if str(row[0])[:10] < today)
        if self.generation() == key[0]:
            self._calendar_snapshot = (key, days)
        return list(days)

    def _freeze_one(self, strategy_id):
        try:
            return self.engine.freeze_one(strategy_id)
        except ValueError as exc:
            raise TrainingError('所选策略不可用，请重新选择') from exc

    def strategies(self):
        return self.engine.list_strategies()

    def freeze(self, ids):
        try:
            return [self._freeze_one(sid) for sid in ids]
        except ValueError as exc:
            raise TrainingError('所选策略不可用，请重新选择') from exc

    def scan(self, strategies, as_of):
        for frozen in strategies:
            if self._freeze_one(frozen['id'])['fingerprint'] != frozen['fingerprint']:
                raise TrainingError('策略已修改，请开始新训练', 409)
        return self.engine.scan([s['id'] for s in strategies], as_of)

    def cache_revision(self):
        return (self.repo.generation,)

    def cached_index(self, strategies):
        return self.engine.cached_index([s['id'] for s in strategies])

    def snapshot_many(self, symbols, start, end):
        # One repository read for the whole batch, then the shared indicator formula.
        begin, stop = date.fromisoformat(start), date.fromisoformat(end)
        warmup = begin - timedelta(days=150)  # same warmup as repository.get_daily
        raw_columns = ['symbol', 'date', 'open', 'high', 'low', 'close', 'volume', 'raw_close', 'raw_high', 'raw_low']
        if hasattr(self.repo, 'read_enriched_panel'):
            # Reuse cached indicators without warming unrelated market history.
            columns = list(dict.fromkeys([*raw_columns, *DISPLAY_FIELDS]))
            frame = self.repo.read_enriched_panel(warmup, stop, columns, symbols=symbols)
        else:
            frame = self.repo.get_enriched_range(warmup, stop, symbols=symbols)
            if frame is None:
                frame = self.repo.get_daily_batch(symbols, warmup, stop, columns=raw_columns)
        if frame.is_empty():
            return {s: [] for s in symbols}
        if not set(raw_columns).issubset(frame.columns):
            raise TrainingError('日线缺少原始价或成交量，不能安全撮合')
        # Retain only this batch and bounded dates even if a repository cache is wider.
        frame = frame.filter(pl.col('symbol').is_in(symbols) & pl.col('date').is_between(warmup, stop))
        if not (set(DISPLAY_FIELDS) - {'ma125'}).issubset(frame.columns):
            frame = compute_indicators(frame.select(raw_columns), needed=set(DISPLAY_FIELDS))
        frame = frame.filter(pl.col('date') >= begin).sort(['symbol', 'date'])
        return {s: self._snapshot_rows(frame.filter(pl.col('symbol') == s)) for s in symbols}

    @staticmethod
    def _snapshot_rows(frame):
        if frame.is_empty():
            return []
        columns = ['date', *[c for c in (*DISPLAY_FIELDS, 'raw_close', 'raw_high', 'raw_low') if c in frame.columns]]
        rows = frame.select(columns).sort('date').unique('date', keep='last', maintain_order=True).to_dicts()
        return [{k: str(v)[:10] if k == 'date' else _number(v) for k, v in row.items()} for row in rows]

    def snapshot_many_with_history(self, symbols, start, end, as_of, count):
        """One raw projection, with the same two independently seeded windows."""
        if not hasattr(self.repo, 'read_enriched_before_many'):
            result = self.snapshot_many(symbols, start, end)
            for symbol, rows in result.items():
                missing = max(0, count-sum(r['date'] <= as_of for r in rows))
                if missing and rows:
                    earlier = self.history_before(symbol, rows[0]['date'], missing)
                    result[symbol] = [*earlier, *rows]
            return result
        begin, stop = date.fromisoformat(start), date.fromisoformat(end)
        warmup = begin-timedelta(days=150)
        raw_columns = ['symbol', 'date', 'open', 'high', 'low', 'close', 'volume', 'raw_close', 'raw_high', 'raw_low']
        frames = self.repo.read_enriched_before_many(
            {s: (date.fromisoformat(as_of)+timedelta(days=1), count) for s in symbols},
            list(dict.fromkeys([*raw_columns, *DISPLAY_FIELDS])), warmup_days=150,
            include_start=warmup, include_end=stop)
        result = {}
        for symbol, frame in frames.items():
            if frame.is_empty():
                result[symbol] = []
                continue
            if not set(raw_columns).issubset(frame.columns):
                raise TrainingError('日线缺少原始价或成交量，不能安全撮合')
            # Reusing a longer EMA/KDJ seed would change already-frozen values.
            initial = frame.filter(pl.col('date').is_between(warmup, stop))
            if not (set(DISPLAY_FIELDS)-{'ma125'}).issubset(initial.columns):
                initial = compute_indicators(initial.select(raw_columns), needed=set(DISPLAY_FIELDS))
            rows = self._snapshot_rows(initial.filter(pl.col('date') >= begin))
            missing = max(0, count-sum(r['date'] <= as_of for r in rows))
            earlier = []
            if missing and rows:
                prior = frame.filter(pl.col('date') < date.fromisoformat(rows[0]['date']))
                dates = prior['date'].drop_nulls().unique().sort().to_list()[-missing:]
                if dates:
                    seed = dates[0]-timedelta(days=150)
                    prior = prior.filter(pl.col('date') >= seed)
                    if not (set(DISPLAY_FIELDS)-{'ma125'}).issubset(prior.columns):
                        prior = compute_indicators(prior.select(raw_columns), needed=set(DISPLAY_FIELDS))
                    earlier = self._snapshot_rows(prior)[-missing:]
            result[symbol] = [*earlier, *rows]
        return result

    def snapshot(self, symbol, start, end):
        frame = self.repo.get_daily(symbol, date.fromisoformat(start), date.fromisoformat(end))
        if frame.is_empty():
            return []
        required = {'date', 'open', 'high', 'low', 'close', 'raw_close', 'raw_high', 'raw_low', 'volume'}
        if not required.issubset(frame.columns):
            raise TrainingError('日线缺少原始价或成交量，不能安全撮合')
        columns = ['date', *[c for c in (*DISPLAY_FIELDS, 'raw_close', 'raw_high', 'raw_low') if c in frame.columns]]
        rows = frame.select(columns).sort('date').unique('date', keep='last', maintain_order=True).to_dicts()
        return [{k: str(v)[:10] if k == 'date' else _number(v) for k, v in row.items()} for row in rows]

    def history_before(self, symbol, before, count):
        """Read missing trading bars with their original indicator warmup."""
        if hasattr(self.repo, 'read_enriched_before'):
            raw_columns = ['symbol', 'date', 'open', 'high', 'low', 'close', 'volume', 'raw_close', 'raw_high', 'raw_low']
            frame = self.repo.read_enriched_before(symbol, date.fromisoformat(before), count,
                list(dict.fromkeys([*raw_columns, *DISPLAY_FIELDS])), warmup_days=150)
            if frame.is_empty():
                return []
            if not set(raw_columns).issubset(frame.columns):
                raise TrainingError('日线缺少原始价或成交量，不能安全撮合')
            if not (set(DISPLAY_FIELDS) - {'ma125'}).issubset(frame.columns):
                frame = compute_indicators(frame.select(raw_columns), needed=set(DISPLAY_FIELDS))
            return self._snapshot_rows(frame)[-count:]
        if hasattr(self.repo, 'read_enriched_dates_before'):
            dates = [(day,) for day in self.repo.read_enriched_dates_before(symbol, date.fromisoformat(before), count)]
        else:
            dates = self.repo.execute_all(
                'SELECT DISTINCT date FROM kline_enriched WHERE symbol=? AND date<? ORDER BY date DESC LIMIT ?',
                [symbol, date.fromisoformat(before), count])
        if not dates:
            return []
        start = min(str(r[0])[:10] for r in dates)
        end = max(str(r[0])[:10] for r in dates)
        rows = self.snapshot_many([symbol], start, end).get(symbol, [])
        return [r for r in rows if start <= r['date'] <= end][-count:]

    def select_topic(self, selection, as_of, calendar):
        return topic_scan(self.repo, selection['type'], as_of, calendar,
                          selection['boards'], selection['exclude_st'])


class TrainingService:
    def __init__(self, store, market):
        self.store, self.market = store, market
        from app.services.training_accounts import TrainingAccounts

        self.accounts = TrainingAccounts(self)

    def preload_revision(self, config, session=None):
        if session is not None:
            revision = (session.get('cached_index') or {}).get('cache_revision')
            return (session['generation'], tuple(s['fingerprint'] for s in session['strategies']),
                    tuple(revision) if revision is not None else None)
        strategies = self.market.freeze(config.get('strategy_ids', [])) if config.get('selection_type', 'strategy') == 'strategy' else []
        revision = self.market.cache_revision() if strategies and config.get('strategy_source', 'live') == 'cached' and hasattr(self.market, 'cache_revision') else None
        return (self.market.generation(), tuple(s['fingerprint'] for s in strategies), revision)

    def create(self, config, progress=None, defer_save=False):
        def report(phase, done=0, total=0):
            if progress:
                progress(phase, done, total)
        report('读取训练配置')
        key = config['request_id']
        if not defer_save:
            with self.store.connect(write=False) as db:
                previous = db.execute('SELECT payload FROM sessions WHERE create_key=?', (key,)).fetchone()
            if previous:
                return self._view(json.loads(previous[0]))
        selection = dict(type=config.get('selection_type', 'strategy'),
                         boards=config.get('boards', sorted(BOARDS)), exclude_st=config.get('exclude_st', False),
                         history_start=config.get('history_start'), history_end=config.get('history_end'))
        practice = config.get('practice_kind', 'screening')
        if selection['type'] not in TYPE_IDS or not selection['boards'] or not set(selection['boards']) <= BOARDS:
            raise TrainingError('选股类型或股票板块不支持')
        if practice not in {'single', 'screening'}:
            raise TrainingError('训练方式不支持')
        if selection['type'] == 'strategy' and not config.get('strategy_ids'):
            raise TrainingError('请选择至少一个当前策略')
        strategies = self.market.freeze(config.get('strategy_ids', [])) if selection['type'] == 'strategy' else []
        source = config.get('strategy_source', 'live') if selection['type'] == 'strategy' else 'live'
        if source not in {'cached', 'live'}:
            raise TrainingError('策略数据来源不支持')
        cached_index = None
        if source == 'cached':
            report('读取已有策略命中缓存')
            cached_index = self.market.cached_index(strategies)
        previous = self.store.load(config['previous_session_id']) if config.get('previous_session_id') and not defer_save else None
        if previous and (not previous['finished'] or previous.get('verification', False) != config.get('verification', False)):
            raise TrainingError('请先结束上一题，再沿用设置开始下一题', 409)
        generation = self.market.generation()
        calendar = self.market.calendar()
        horizon = config.get('horizon', 60)
        if len(calendar) < 120 + horizon:
            raise TrainingError('历史不足，需要至少 120 根观察日线及后续训练行情')
        # Sample market date before any strategy/candidate returns are examined.
        start, end = selection['history_start'], selection['history_end']
        if start and end and start > end:
            raise TrainingError('历史区间起始日期不能晚于结束日期')
        # Unset bounds retain the original recent-750-day sampling scope.
        eligible = [i for i in range(119 if cached_index is not None or start or end else max(119, len(calendar) - 750), len(calendar) - horizon)
                    if (not start or calendar[i] >= start) and (not end or calendar[i] <= end)
                    and (cached_index is None or calendar[i] in cached_index['days'])]
        signal_start = bool(strategies)
        if signal_start and cached_index is not None:
            # T+0 must have a matching signal under the selected board/ST filters.
            eligible = [i for i in eligible if filter_universe(
                [hit for hits in cached_index['days'][calendar[i]].values() for hit in hits],
                selection['boards'], selection['exclude_st'])]
        if not eligible:
            if cached_index is not None:
                raise TrainingError('所选策略、历史区间或训练长度没有可用缓存起点。请缩短训练长度、扩大区间，或在策略页补全历史命中缓存；未自动重算', 409)
            raise TrainingError('所选历史区间不足以提供观察窗口及后续训练行情')
        begin = random.SystemRandom().choice(eligible)
        count = 1 if practice == 'single' else config.get('candidate_count', 3)
        if not 1 <= count <= 20:
            raise TrainingError('每批候选数量应为1至20')
        scan_min, scan_max = config.get('scan_interval_min', 3), config.get('scan_interval_max', 10)
        if not 1 <= scan_min <= scan_max <= 30:
            raise TrainingError('重新选股间隔应为1至30，最小值不能超过最大值')
        session = dict(id=uuid.uuid4().hex, revision=0, mode=config.get('mode', 'close_t'),
                       strategies=strategies, generation=generation, dates=calendar[begin:begin + horizon + 1],
                       warmup_start=calendar[max(0, begin - 250)], cursor=0, assets={}, active=[],
                       candidate_count=count, auto_scan=practice != 'single' and config.get('auto_scan', True),
                       practice_kind=practice, selection=selection, selection_version=1,
                       strategy_source=source,
                       cached_index={**cached_index, 'days': {d: hits for d, hits in cached_index['days'].items()
                                      if calendar[begin] <= d <= calendar[begin + horizon]}} if cached_index else None,
                       scan_interval_min=scan_min, scan_interval_max=scan_max,
                       question_number=previous.get('question_number', 1) + 1 if previous else 1,
                       previous_session_id=previous['id'] if previous else None,
                       next_scan=random.SystemRandom().randint(3, 10), indicators=list(dict.fromkeys(config.get('indicators', ['vol', 'macd']))),
                       cash=100000.0, initial=100000.0, position=None, pending=None, events=[],
                       trades=[], fills=[], notices=[], equity=[dict(day=0, value=100000)], finished=False,
                       costs=dict(commission_pct=.0002, stamp_tax_pct=.0005, slippage_bps=5),
                       scans=[], rejected=[], analyses=[], verification=config.get('verification', False))
        exclude = {a['symbol'] for a in previous['assets'].values()} if previous and previous['dates'][0] == calendar[begin] else set()
        history_bars = config.get('history_bars', 120)
        if history_bars not in {120, 250, 500}:
            raise TrainingError('历史根数仅支持120、250或500')
        self._scan(session, excluded=exclude, progress=progress, history_bars=history_bars)
        if signal_start and cached_index is not None:
            if not session['active']:
                raise TrainingError('所选起始日没有可训练的策略信号股票（需当日行情和至少120根历史），请调整日期或股票范围', 409)
            session['notices'].append('T+0 为策略选股信号当天，已展示当天完整日线；推进后进入下一交易日')
        for index, alias in enumerate(session['active']):
            asset = session['assets'][alias]
            asset['history_window'] = history_bars
            known = sum(r['date'] <= session['dates'][0] for r in asset['rows'])
            missing = max(0, history_bars - known)
            if missing and not asset.get('history_loaded'):
                report(f'预加载所选 {history_bars} 根历史K线', index, len(session['active']))
                first = asset['rows'][0]['date']
                earlier = [r for r in self.market.history_before(asset['symbol'], first, missing) if r['date'] < first][-missing:]
                asset['rows'] = sorted({r['date']: r for r in [*earlier, *asset['rows']]}.values(), key=lambda r: r['date'])
                asset['snapshot_hash'] = _hash(asset['rows'])
                asset['history_loaded'] = len(earlier) < missing
        report('保存匿名训练题目')
        if self.market.generation() != generation:
            raise TrainingError('数据版本正在更新，请稍后开始训练', 409)
        if defer_save:
            return session  # server-only staged question, absent from history/statistics
        with self.store.connect() as db:
            # A concurrently repeated create is idempotent too.
            previous = db.execute('SELECT payload FROM sessions WHERE create_key=?', (key,)).fetchone()
            if previous:
                return self._view(json.loads(previous[0]))
            if config.get('continuous_account'):
                self.accounts.attach(db, session, previous_id=config.get('previous_session_id'),
                                     expected_account_id=config.get('account_id'))
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (session['id'], key, _dump(session)))
        return self._view(session)

    def activate_preloaded(self, session, config, progress=None):
        """Claim an owned, staged question only after generation/rules are rechecked."""
        if self.market.generation() != session['generation']:
            return None
        if session['strategies']:
            current = self.market.freeze(config.get('strategy_ids', []))
            if [s['fingerprint'] for s in current] != [s['fingerprint'] for s in session['strategies']]:
                return None
        if session.get('strategy_source') == 'cached' and hasattr(self.market, 'cache_revision'):
            revision = self.market.cache_revision()
            if session['cached_index'].get('cache_revision') != (list(revision) if revision else None):
                return None
        previous = self.store.load(config['previous_session_id']) if config.get('previous_session_id') else None
        if previous:
            if not previous['finished'] or previous.get('verification', False) != config.get('verification', False):
                raise TrainingError('请先结束上一题，再沿用设置开始下一题', 409)
            if previous['dates'][0] == session['dates'][0]:
                symbols = {a['symbol'] for a in previous['assets'].values()}
                if any(a['symbol'] in symbols for a in session['assets'].values()):
                    return None  # original next-topic exclusion semantics still apply
        if any(asset.get('history_window', 120) != config.get('history_bars', 120) for asset in session['assets'].values()):
            return None  # An older ready topic cannot claim a newly selected window.
        session['indicators'] = list(dict.fromkeys(config.get('indicators', ['vol', 'macd'])))
        session['previous_session_id'] = previous['id'] if previous else None
        session['question_number'] = previous.get('question_number', 1) + 1 if previous else 1
        if progress:
            progress('打开已预加载的匿名题目')
        with self.store.connect() as db:
            existing = db.execute('SELECT payload FROM sessions WHERE create_key=?', (config['request_id'],)).fetchone()
            if existing:
                return self._view(json.loads(existing[0]))
            if self.market.generation() != session['generation']:
                return None
            if config.get('continuous_account'):
                self.accounts.attach(db, session, previous_id=config.get('previous_session_id'),
                                     expected_account_id=config.get('account_id'))
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (session['id'], config['request_id'], _dump(session)))
        return self._view(session)

    def _row(self, session, alias, day=None):
        as_of = session['dates'][session['cursor'] if day is None else day]
        return next((r for r in session['assets'][alias]['rows'] if r['date'] == as_of), None)

    def _last_row(self, session, alias):
        as_of = session['dates'][session['cursor']]
        return next((r for r in reversed(session['assets'][alias]['rows']) if r['date'] <= as_of), None)

    def _scan(self, session, excluded=None, progress=None, history_bars=120):
        def report(phase, done=0, total=0):
            if progress:
                progress(phase, done, total)
        if self.market.generation() != session['generation']:
            raise TrainingError('数据版本已变化，请结束本场后开始新训练', 409)
        as_of = session['dates'][session['cursor']]
        selection = session.get('selection', dict(type='strategy', boards=sorted(BOARDS), exclude_st=False))
        if selection['type'] == 'strategy':
            if session.get('strategy_source') == 'cached':
                report('读取历史缓存候选')
                hits = session['cached_index']['days'].get(as_of)
                if hits is None:
                    raise TrainingError('当前训练日未覆盖所选策略的命中缓存，请暂停重选或结束本题；未自动重算', 409)
                candidates = list({row[0]: row[1] for sid in hits for row in hits[sid]}.items())
            else:
                report('按当前策略计算历史信号（较慢）')
                candidates = self.market.scan(session['strategies'], as_of)
        else:
            candidates = self.market.select_topic(selection, as_of, self.market.calendar())
        pool_size = len(candidates)
        candidates = filter_universe(candidates, selection['boards'], selection['exclude_st'])
        candidates = [c for c in candidates if c[0] not in (excluded or set())]
        random.SystemRandom().shuffle(candidates)
        active = []
        inspected = 0
        batch_rows = {}
        for index, (symbol, name) in enumerate(candidates[:400]):
            inspected += 1
            existing = next((a for a, value in session['assets'].items() if value['symbol'] == symbol), None)
            if existing:
                if self._row(session, existing):
                    active.append(existing)
            elif len(session['assets']) < 120:
                # Future stays server-side in a frozen snapshot; whitelist serialization below.
                report('批量载入日线与可见指标', len(active), session['candidate_count'])
                if hasattr(self.market, 'snapshot_many'):
                    if symbol not in batch_rows:
                        pending_symbols = [s for s, _ in candidates[index:index + session['candidate_count'] - len(active)]
                                           if not any(a['symbol'] == s for a in session['assets'].values())]
                        if history_bars > 120 and hasattr(self.market, 'snapshot_many_with_history'):
                            batch_rows.update(self.market.snapshot_many_with_history(
                                pending_symbols, session['warmup_start'], session['dates'][-1], as_of, history_bars))
                        else:
                            batch_rows.update(self.market.snapshot_many(pending_symbols, session['warmup_start'], session['dates'][-1]))
                    rows = batch_rows.get(symbol, [])
                else:
                    rows = self.market.snapshot(symbol, session['warmup_start'], session['dates'][-1])
                known = [r for r in rows if session['warmup_start'] <= r['date'] <= as_of
                         and (_number(r.get('close')) or 0) > 0]
                if len(known) < 120 or known[-1]['date'] != as_of:
                    continue
                alias = chr(65 + len(session['assets'])) if len(session['assets']) < 26 else f"票{len(session['assets']) + 1}"
                scale = 100 / known[-120]['close']
                session['assets'][alias] = dict(symbol=symbol, name=name, scale=scale, rows=rows,
                                                snapshot_hash=_hash(rows))
                if history_bars > 120 and hasattr(self.market, 'snapshot_many_with_history'):
                    session['assets'][alias]['history_loaded'] = sum(r['date'] <= as_of for r in rows) < history_bars
                active.append(alias)
            if len(active) == session['candidate_count']:
                break
        report('匿名化图表并校验历史边界', len(active), session['candidate_count'])
        if self.market.generation() != session['generation']:
            raise TrainingError('数据版本正在更新，请稍后重试', 409)
        session['active'], session['rejected'] = active, []
        session['scans'].append(dict(day=session['cursor'], pool_size=pool_size, eligible_size=len(candidates),
                                    selected=list(active), inspected=inspected))
        session['next_scan'] = session['cursor'] + random.SystemRandom().randint(
            session.get('scan_interval_min', 3), session.get('scan_interval_max', 10))
        session['notices'].append(f"T+{session['cursor']} 选股完成，候选 {len(active)} 只" if active else '本次设置无可用候选，可以继续推进或结束空题')
        if len(active) < session['candidate_count'] and (inspected == 400 or len(session['assets']) >= 120):
            session['notices'].append('本题已到候选检查或累计股票数量上限；可结束后进入下一题')
        if session.get('strategy_source') == 'cached':
            session['notices'].append('题目来自已计算的历史命中缓存；已记录构建策略与行情版本'
                                     if session.get('cached_index', {}).get('parameter_version') == 'recorded'
                                     else '题目来自已计算的历史命中缓存；旧缓存未记录参数版本，不能视为当前参数的重算结果')

    def _bars(self, session, alias, tail=None):
        asset = session['assets'][alias]
        as_of = session['dates'][session['cursor']]
        known = [r for r in asset['rows'] if r['date'] <= as_of]
        dates = sorted({r['date'] for r in asset['rows'] if r['date'] <= as_of} | set(session['dates'][:session['cursor'] + 1]))
        base = dates.index(session['dates'][0])
        offsets = {d: i - base for i, d in enumerate(dates)}
        output = []
        window = asset.get('history_window', 'all' if asset.get('history_loaded') else 120)
        visible = known if window == 'all' else known[-window:]
        for row in visible[-tail:] if tail else visible:
            offset = offsets[row['date']]
            d = dict(date=f'T{offset:+d}')
            for key in DISPLAY_FIELDS:
                value = _number(row.get(key))
                d[key] = value
            d['raw_close'] = _number(row.get('raw_close'))
            output.append(d)
        return output

    def expand_history(self, session_id, command):
        """Load only earlier bars outside the write lock, then freeze atomically."""
        session = self.store.load(session_id)
        with self.store.connect(write=False) as db:
            if db.execute('SELECT 1 FROM commands WHERE session_id=? AND request_id=?',
                          (session_id, command['request_id'])).fetchone():
                return self._view(self.store.read(db, session_id))
        if session['revision'] != command['revision']:
            raise TrainingError('训练版本已变化，请刷新后重试', 409)
        alias = command['alias']
        visible = self._visible_aliases(session)
        if alias not in visible:
            raise TrainingError('请选择当前可见候选')
        asset = session['assets'][alias]
        count = command.get('bars')
        if count is not None and count not in {120, 250, 500}:
            raise TrainingError('历史根数仅支持120、250、500或全部')
        as_of = session['dates'][session['cursor']]
        known_count = sum(r['date'] <= as_of for r in asset['rows'])
        missing = max(0, count - known_count) if count else 0
        needs_read = not asset.get('history_loaded') and (count is None or missing > 0)
        rows = []
        complete = asset.get('history_loaded', False)
        if needs_read:
            if self.market.generation() != session['generation']:
                raise TrainingError('数据版本已变化，无法把新行情混入原题，请结束后开始新题', 409)
            # Local A-share history, through the existing repository contract.
            first = min(r['date'] for r in asset['rows'])
            end = (date.fromisoformat(first) - timedelta(days=1)).isoformat()
            if count is not None:
                rows = self.market.history_before(asset['symbol'], first, missing)
            elif hasattr(self.market, 'snapshot_many'):
                rows = self.market.snapshot_many([asset['symbol']], '1900-01-01', end).get(asset['symbol'], [])
            else:
                rows = self.market.snapshot(asset['symbol'], '1900-01-01', end)
            # Defend against a wider cache; frozen overlapping/future bars win.
            rows = [r for r in rows if r['date'] < first]
            if count is not None:
                rows = rows[-missing:]
            complete = count is None or len(rows) < missing
            if self.market.generation() != session['generation']:
                raise TrainingError('数据版本正在更新，请稍后重试', 409)
        with self.store.connect() as db:
            current = self.store.read(db, session_id)
            if db.execute('SELECT 1 FROM commands WHERE session_id=? AND request_id=?',
                          (session_id, command['request_id'])).fetchone():
                return self._view(current)
            if current['revision'] != command['revision']:
                raise TrainingError('训练版本已变化，请刷新后重试', 409)
            target = current['assets'][alias]
            window = count if count is not None else 'all'
            if rows or target.get('history_window', 'all' if target.get('history_loaded') else 120) != window or complete != target.get('history_loaded', False):
                if needs_read and self.market.generation() != current['generation']:
                    raise TrainingError('数据版本正在更新，请稍后重试', 409)
                target['rows'] = sorted({r['date']: r for r in [*rows, *target['rows']]}.values(), key=lambda r: r['date'])
                target['snapshot_hash'] = _hash(target['rows'])
                target['history_loaded'] = complete
                target['history_window'] = window
                current['revision'] += 1
            db.execute('UPDATE sessions SET payload=? WHERE id=?', (_dump(current), session_id))
            db.execute('INSERT INTO commands VALUES (?,?)', (session_id, command['request_id']))
        return self._view(current)

    @staticmethod
    def _price_factor(row):
        close, raw = _number(row.get('close')), _number(row.get('raw_close'))
        if close is None or raw is None or close <= 0 or raw <= 0:
            raise TrainingError('缺少有效复权或原始价格，持仓保留，请检查本地行情')
        return close / raw

    def _position_units(self, session):
        pos = session['position']
        units = _number(pos.get('adjusted_units'))
        if units is not None and units > 0:
            return units
        # Legacy holdings derive the basis from their frozen entry bar; no migration.
        entry = self._row(session, pos['alias'], pos['entry_day'])
        if entry is None:
            raise TrainingError('持仓买入日复权行情缺失，持仓保留，请检查本地行情')
        return pos['shares'] / self._price_factor(entry)

    def _position_value(self, session, row, field='close'):
        value = _number(row.get(field))
        if value is None or value <= 0:
            raise TrainingError('持仓复权行情缺失，持仓保留，请检查本地行情')
        return self._position_units(session) * value

    def _equity(self, session):
        pos = session['position']
        value = session['cash']
        if pos:
            value += self._position_value(session, self._last_row(session, pos['alias']))
        return value

    @staticmethod
    def _record_group(session):
        strategies = session.get('strategies', [])
        if strategies:
            ids = sorted({strategy['id'] for strategy in strategies if strategy.get('id')})
            ordered = sorted(strategies, key=lambda strategy: strategy.get('id', strategy.get('name', '')))
            names = [strategy.get('name', strategy.get('id', '未记录名称')) for strategy in ordered]
            return dict(id=('strategy:' + _dump(ids)) if ids else 'legacy-strategy:' + _dump(sorted(names)),
                        name=('多策略组合 · ' if len(strategies) > 1 else '') + '、'.join(names))
        topic = session.get('selection', {}).get('type', 'strategy')
        return dict(id='selection:' + topic, name=selection_name(topic))

    @staticmethod
    def _visible_aliases(session):
        pos, pending = session['position'], session['pending']
        return list(dict.fromkeys([*session['active'], *([pos['alias']] if pos else []),
                                  *([pending['alias']] if pending else [])]))

    def _view(self, session, bar_tail=None, include_account=True):
        pos = session['position']
        pending = session['pending']
        aliases = self._visible_aliases(session)
        candidates = [dict(alias=a, bars=self._bars(session, a, tail=bar_tail), tradable=self._row(session, a) is not None,
                           rejected=a in session['rejected'], held=bool(pos and pos['alias'] == a),
                           history_loaded=session['assets'][a].get('history_loaded', False),
                           history_window=session['assets'][a].get('history_window',
                              'all' if session['assets'][a].get('history_loaded') else 120)) for a in aliases]
        view = dict(id=session['id'], replay_of=session.get('replay_of'), replay_number=session.get('replay_number', 0), price_unit='cny_qfq', record_group=self._record_group(session), revision=session['revision'], mode=session['mode'], cursor=session['cursor'],
                    horizon=len(session['dates']) - 1, finished=session['finished'], candidates=candidates,
                    strategies=[dict(id=s['id'], name=s['name']) for s in session['strategies']],
                    indicators=session['indicators'], cash=session['cash'], equity=self._equity(session),
                    total_return=self._equity(session) / session['initial'] - 1,
                    position=dict(alias=pos['alias'], entry_day=pos['entry_day'],
                                  cost_price=pos['cost'] / self._position_units(session),
                                  pnl=self._position_value(session, self._last_row(session, pos['alias'])) / pos['cost'] - 1,
                                  entry_price=pos['display_price'] if pos.get('display_price_unit') == 'cny_qfq'
                                  else pos['display_price'] / session['assets'][pos['alias']]['scale']) if pos else None,
                    pending={k: pending[k] for k in ('action', 'alias', 'day')} if pending else None,
                    events=[{**{k: e[k] for k in ('id', 'day', 'action', 'alias', 'reason', 'tags', 'outcome')},
                             **{k: e.get(k, default) for k, default in (('invalidation', ''), ('confidence', 3), ('size', .5), ('pattern', ''))}}
                            for e in session['events']],
                    trades=copy.deepcopy(session['trades']), notices=session['notices'][-6:],
                    costs=session['costs'], auto_scan=session['auto_scan'], verification=session.get('verification', False),
                    practice_kind=session.get('practice_kind', 'screening'), question_number=session.get('question_number', 1),
                    strategy_source=session.get('strategy_source', 'live'),
                    selection={**copy.deepcopy(session.get('selection', dict(type='strategy', boards=sorted(BOARDS), exclude_st=False))),
                               'name': selection_name(session.get('selection', {}).get('type', 'strategy'))},
                    candidate_count=session['candidate_count'], scan_interval_min=session.get('scan_interval_min', 3),
                    scan_interval_max=session.get('scan_interval_max', 10),
                    scan_info=copy.deepcopy(session['scans'][-1]) if session['scans'] else None)
        if include_account and session.get('account_id'):
            view['account'] = self.accounts.session_snapshot(session)
            view['account_question'] = session['account_question']
            view['account_settlement'] = copy.deepcopy(session.get('account_settlement'))
        if session['finished']:
            view['review'] = self._review(session)
        return view

    def view(self, session_id):
        return self._view(self.store.load(session_id))

    def list(self):
        return [dict(id=s['id'], replay_of=s.get('replay_of'), replay_number=s.get('replay_number', 0), record_group=self._record_group(s), mode=s['mode'], cursor=s['cursor'], finished=s['finished'],
                     strategies=[v['name'] for v in s['strategies']], total_return=self._equity(s) / s['initial'] - 1,
                     verification=s.get('verification', False), selection_type=s.get('selection', {}).get('type', 'strategy'),
                     selection_name=selection_name(s.get('selection', {}).get('type', 'strategy')),
                     practice_kind=s.get('practice_kind', 'screening'), question_number=s.get('question_number', 1))
                for s in self.store.list_for_display()]

    def replay(self, session_id, command):
        """Start one previously revealed stock again using its frozen question."""
        key = f"replay:{session_id}:{command['request_id']}"
        with self.store.connect() as db:
            existing = db.execute('SELECT payload FROM sessions WHERE create_key=?', (key,)).fetchone()
            if existing:
                previous = json.loads(existing[0])
                if previous['active'][0] != command['alias']:
                    raise TrainingError('相同请求标识不能重训不同股票', 409)
                return self._view(previous)
            original = self.store.read(db, session_id)
            if not original['finished']:
                raise TrainingError('请先结束并揭示，再重训本题股票', 409)
            if original['revision'] != command['revision']:
                raise TrainingError('训练版本已变化，请刷新后重试', 409)
            alias = command['alias']
            if alias not in original['assets']:
                raise TrainingError('请选择本题股票')
            # No market or strategy refresh: a replay keeps the exact evidence,
            # costs and full planned interval even if current data has changed.
            session = copy.deepcopy(original)
            root = original.get('replay_root') or session_id
            number = db.execute("SELECT coalesce(max(json_extract(payload,'$.replay_number')),0)+1 "
                                "FROM sessions WHERE json_extract(payload,'$.replay_root')=?", (root,)).fetchone()[0]
            session.update(id=uuid.uuid4().hex, revision=0, cursor=0, finished=False,
                           replay_of=session_id, replay_root=root, replay_number=number,
                           assets={alias: session['assets'][alias]}, active=[alias], rejected=[],
                           candidate_count=1, practice_kind='single', auto_scan=False,
                           position=None, pending=None, cash=session['initial'], events=[], fills=[], trades=[], analyses=[],
                           equity=[dict(day=0, value=session['initial'])], previous_session_id=None,
                           account_id=None, account_question=None, account_settlement=None,
                           scans=[dict(day=0, pool_size=1, eligible_size=1, selected=[alias], inspected=1)],
                           notices=['重训已看过结果的同一只股票，从T+0重新开始；沿用原题冻结行情，重训单独汇总。'])
            if command.get('continuous_account'):
                self.accounts.attach(db, session, expected_account_id=command.get('account_id'))
            db.execute('INSERT INTO sessions VALUES (?,?,?)', (session['id'], key, _dump(session)))
        return self._view(session)

    def _future_outcomes(self, session):
        """Post-training excursions from T+0 close to the frozen planned end."""
        dates = session['dates']
        offsets = {day: index for index, day in enumerate(dates)}
        output = []
        for alias, asset in session['assets'].items():
            rows = [r for r in asset['rows'] if r['date'] in offsets]
            base = next((r for r in rows if r['date'] == dates[0]), None)
            close = _number(base.get('close')) if base else None
            result = dict(alias=alias, base_day=0, base_date=dates[0], base_close=close,
                          final_day=len(dates) - 1, final_date=dates[-1], max_rise=None, max_fall=None,
                          max_rise_day=None, max_fall_day=None, max_rise_price=None, max_fall_price=None,
                          final_close=None, final_return=None, last_available_day=None, last_available_date=None,
                          data_status='missing_start')
            if close is not None and close > 0:
                valid = [r for r in rows if (_number(r.get('close')) or 0) > 0]
                last = max(valid, key=lambda r: r['date'])
                future = [r for r in rows if r['date'] > dates[0]]
                highs = [(_number(r.get('high')), offsets[r['date']]) for r in future if (_number(r.get('high')) or 0) > 0]
                lows = [(_number(r.get('low')), offsets[r['date']]) for r in future if (_number(r.get('low')) or 0) > 0]
                # T+0 intraday extremes happened before the reference close.
                # Including the close itself makes a never-negative gain/loss 0.
                if highs:
                    high, high_day = max([(close, 0), *highs], key=lambda item: item[0])
                    result.update(max_rise=high / close - 1, max_rise_day=high_day, max_rise_price=high)
                if lows:
                    low, low_day = min([(close, 0), *lows], key=lambda item: item[0])
                    result.update(max_fall=low / close - 1, max_fall_day=low_day, max_fall_price=low)
                if len(highs) != len(future):
                    result.update(max_rise=None, max_rise_day=None, max_rise_price=None)
                if len(lows) != len(future):
                    result.update(max_fall=None, max_fall_day=None, max_fall_price=None)
                status = 'complete' if last['date'] == dates[-1] else 'last_available'
                if status == 'complete' and (len(highs) != len(future) or len(lows) != len(future)):
                    status = 'partial_prices'
                result.update(final_close=last['close'], final_return=last['close'] / close - 1,
                              last_available_day=offsets[last['date']], last_available_date=last['date'], data_status=status)
            output.append(result)
        return output

    def _review(self, session):
        trades = session['trades']
        peak, drawdown = session['initial'], 0.0
        for point in session['equity']:
            peak = max(peak, point['value'])
            drawdown = min(drawdown, point['value'] / peak - 1)
        return dict(identities=[dict(alias=a, symbol=value['symbol'], name=value['name'])
                                for a, value in session['assets'].items()],
                    start=session['dates'][0], end=session['dates'][session['cursor']],
                    planned_end=session['dates'][-1], future_outcomes=self._future_outcomes(session),
                    replay_of=session.get('replay_of'), replay_number=session.get('replay_number', 0),
                    stats=dict(trades=len(trades), win_rate=sum(t['pnl'] > 0 for t in trades) / len(trades) if trades else None,
                               total_return=self._equity(session) / session['initial'] - 1,
                               max_drawdown=drawdown, open_positions=int(session['position'] is not None)),
                    events=copy.deepcopy(session['events']), fills=copy.deepcopy(session['fills']),
                    trades=copy.deepcopy(trades), equity=copy.deepcopy(session['equity']),
                    scans=copy.deepcopy(session['scans']), selection=copy.deepcopy(session.get('selection', {})),
                    strategy_source=session.get('strategy_source', 'live'),
                    price_units=dict(chart='cny_qfq', legacy_snapshot_default='normalized_100',
                                     legacy_fill_default='normalized_100',
                                     legacy_scales={alias: asset['scale'] for alias, asset in session['assets'].items()}),
                    cache_provenance={k: v for k, v in (session.get('cached_index') or {}).items() if k != 'days'},
                    strategy_versions_role='reference_only' if session.get('strategy_source') == 'cached' else 'executed_rules',
                    selection_version=session.get('selection_version', 1), practice_kind=session.get('practice_kind', 'screening'),
                    strategy_versions=copy.deepcopy(session['strategies']), data_generation=session['generation'],
                    analyses=copy.deepcopy(session.get('analyses', [])))

    def save_analysis(self, session_id, content):
        with self.store.connect() as db:
            session = self.store.read(db, session_id)
            if not session['finished']:
                raise TrainingError('结束后才可保存复盘', 409)
            session.setdefault('analyses', []).append(dict(content=content, prompt_version='training-v2'))
            session['revision'] += 1
            db.execute('UPDATE sessions SET payload=? WHERE id=?', (_dump(session), session_id))

    def export(self, session_id):
        session = self.store.load(session_id)
        if not session['finished']:
            raise TrainingError('训练结束后才可导出并揭示信息', 409)
        return dict(schema_version=1, id=session_id, mode=session['mode'], costs=session['costs'],
                    initial=session['initial'], **self._review(session))

    def analysis_evidence(self, session_id):
        payload = self.export(session_id)
        evidence = dict(mode=payload['mode'], is_replay=bool(payload.get('replay_of')), stats=payload['stats'], costs=payload['costs'],
                        trades=payload['trades'][-100:], events=[], total_events=len(payload['events']),
                        price_unit='每条known_chart.price_unit标注价格单位：cny_qfq为前复权元，normalized_100为旧归一值；volume沿用TSP手数口径')
        size = 0
        # Preserve complete original reasons for included events. Long sessions expose
        # omission counts and retain the full source in the downloadable review.
        for event in reversed(payload['events'][-20:]):
            item = {k: event[k] for k in ('id', 'day', 'action', 'alias', 'reason', 'tags', 'invalidation', 'confidence', 'size', 'outcome')}
            bars = event['snapshot']['bars']
            if bars:
                volumes = [b['volume'] for b in bars[-6:-1] if b.get('volume') is not None]
                mean_volume = sum(volumes) / len(volumes) if volumes else None
                item['known_chart'] = dict(
                    known_day=event['snapshot']['known_day'], price_unit=event['snapshot'].get('price_unit', 'normalized_100'),
                    indicators=event['snapshot']['indicators'],
                    bars=len(bars), last_bar=bars[-1],
                    recent_five=[{k: b[k] for k in ('date', 'open', 'high', 'low', 'close', 'volume')} for b in bars[-5:]],
                    range_high=max(b['high'] for b in bars), range_low=min(b['low'] for b in bars),
                    window_return=bars[-1]['close'] / bars[0]['close'] - 1,
                    volume_ratio_prior5=bars[-1]['volume'] / mean_volume if mean_volume and bars[-1].get('volume') is not None else None)
            item_size = len(_dump(item))
            if evidence['events'] and size + item_size > 20000:
                break
            evidence['events'].insert(0, item)
            size += item_size
        evidence['included_events'] = len(evidence['events'])
        evidence['omitted_events'] = evidence['total_events'] - evidence['included_events']
        return evidence

    def _event(self, session, command):
        alias = command.get('alias')
        reason = str(command.get('reason') or '').strip()
        if not reason:
            raise TrainingError('请先记录本次判断的理由')
        if alias not in session['assets']:
            raise TrainingError('请选择本场候选股票')
        bars = self._bars(session, alias)
        snapshot = dict(bars=bars, price_unit='cny_qfq', indicators=list(session['indicators']), known_day=session['cursor'],
                        mode=session['mode'], strategy_versions=[s['fingerprint'] for s in session['strategies']])
        event = dict(id=f"E{len(session['events']) + 1}", day=session['cursor'], action=command['action'], alias=alias,
                     reason=reason, pattern=str(command.get('pattern') or '').strip()[:80],
                     tags=command.get('tags', []), invalidation=command.get('invalidation', ''),
                     confidence=command.get('confidence', 3), size=command.get('size', .5),
                     outcome='已记录', snapshot=snapshot, snapshot_hash=_hash(snapshot))
        session['events'].append(event)
        return event

    def _fill(self, session, order, day, price_field):
        alias, action = order['alias'], order['action']
        row = self._row(session, alias, day)
        event = next(e for e in session['events'] if e['id'] == order['event_id'])
        if row is None or (_number(row.get('volume')) or 0) <= 0:
            event['outcome'] = '未成交：停牌或缺少当日行情'
            session['notices'].append(event['outcome'])
            return
        factor = self._price_factor(row)
        prior = next((r for r in reversed(session['assets'][alias]['rows']) if r['date'] < row['date']), None)
        if prior is None:
            raise TrainingError('缺少前日原始价，不能判断成交')
        prior_factor = self._price_factor(prior)
        # Quote bounds remain in raw-price units; ex-date reference uses today's basis.
        reference = prior['close'] / factor if abs(factor / prior_factor - 1) > .0001 else prior['raw_close']
        price = (_number(row.get(price_field)) or 0) / factor
        if price <= 0:
            raise TrainingError('缺少有效成交价格')
        asset = session['assets'][alias]
        limit = price_limit_pct(asset['symbol'], date.fromisoformat(row['date']),
                                is_risk_warning=is_risk_warning_name(asset['name']))
        bound = float(numpy_limit_price(np.array([reference]), np.array([limit]), up=action == 'buy')[0])
        # At the opening phase only open and yesterday's bars are available. Reject
        # the limit-priced open even if later high/low proves it unlocked.
        blocked = price >= bound - .005 if action == 'buy' else price <= bound + .005
        if blocked:
            event['outcome'] = '未成交：成交时点处于涨跌停价格'
            session['notices'].append(event['outcome'])
            return
        cost_model = MatcherConfig(**session['costs'])
        pos = session['position']
        if action == 'buy':
            cost_rate = cost_model.buy_cost_pct()
            budget = session['cash'] * event['size']
            shares = math.floor(budget / (price * (1 + cost_rate)) / 100) * 100
            if shares < 100:
                event['outcome'] = '未成交：资金不足一手'
                session['notices'].append(event['outcome'])
                return
            gross = price * shares
            cost = gross * (1 + cost_rate)
            session['cash'] -= cost
            session['position'] = dict(alias=alias, shares=shares, adjusted_units=shares / factor, cost=cost, entry_day=day,
                                       entry_event=event['id'], display_price=row[price_field], display_price_unit='cny_qfq',
                                       fees=gross * cost_rate)
            fees = gross * cost_rate
        else:
            if pos is None or pos['alias'] != alias or day <= pos['entry_day']:
                raise TrainingError('持仓需遵守 T+1 后才能卖出')
            gross = self._position_value(session, row, price_field)
            fees = gross * cost_model.sell_cost_pct()
            net = gross - fees
            session['cash'] += net
            session['trades'].append(dict(alias=alias, entry_day=pos['entry_day'], exit_day=day,
                                          entry_event=pos['entry_event'], exit_event=event['id'],
                                          pnl=net / pos['cost'] - 1, profit=net - pos['cost'],
                                          fees=fees + pos['fees'], duration=day - pos['entry_day']))
            session['position'] = None
        event['outcome'] = f'T+{day} 已成交'
        session['fills'].append(dict(event_id=event['id'], alias=alias, action=action, day=day,
                                     display_price=row[price_field], display_price_unit='cny_qfq', fees=fees))

    def _command_view(self, session, command, simple_step):
        # Opt-in transport delta only for one committed, unchanged candidate step.
        # Retried older commands fall back to a full view for safe resynchronization.
        delta = (simple_step and command.get('step_delta', False) and command.get('price_unit') == 'cny_qfq'
                 and session['revision'] == command['revision'] + 1)
        view = self._view(session, bar_tail=1 if delta else None)
        if delta:
            view.update(step_delta=True, base_revision=command['revision'])
        return view

    def command(self, session_id, command):
        with self.store.connect() as db:
            session = self.store.read_step(db, session_id) if command['action'] == 'advance' else None
            simple_step = session is not None
            if simple_step and command.get('continuous_account') and not session.get('account_id'):
                session = None
                simple_step = False
            if session is None:
                session = self.store.read(db, session_id)
            request_id = command['request_id']
            if db.execute('SELECT 1 FROM commands WHERE session_id=? AND request_id=?', (session_id, request_id)).fetchone():
                return self._command_view(session, command, simple_step)
            if session['revision'] != command['revision']:
                raise TrainingError('训练版本已变化，请刷新后重试', 409)
            if session['finished']:
                raise TrainingError('本场训练已结束', 409)
            if session.get('account_id'):
                self.accounts.guard(db, session)
            elif command.get('continuous_account'):
                self.accounts.attach(db, session, adopt=True)
            action = command['action']
            if action == 'indicators':
                indicators = command.get('indicators', [])
                if len(set(indicators)) != len(indicators) or not set(indicators) <= INDICATORS:
                    raise TrainingError('指标配置不支持')
                session['indicators'] = list(indicators)
            elif action == 'scan':
                if session.get('practice_kind') == 'single':
                    raise TrainingError('单票练习保留同一题，请结束复盘后开始下一题')
                self._scan(session)
            elif action == 'advance':
                if session['cursor'] >= len(session['dates']) - 1:
                    raise TrainingError('已到训练区间末尾，请结束并复盘')
                session['cursor'] += 1
                # Fill with next open before constructing/returning the completed next bar.
                if session['pending']:
                    self._fill(session, session['pending'], session['cursor'], 'open')
                    session['pending'] = None
                if session['auto_scan'] and session['cursor'] >= session['next_scan']:
                    try:
                        rescanned = copy.deepcopy(session)
                        self._scan(rescanned)
                        session = rescanned
                    except TrainingError:
                        session['auto_scan'] = False
                        session['notices'].append('自动选股已暂停，当前行情仍可训练；请手动选股查看原因')
            elif action == 'finish':
                if session['pending']:
                    previous = next(e for e in session['events'] if e['id'] == session['pending']['event_id'])
                    previous['outcome'] = '结束时取消，未成交'
                session['pending'] = None
                session['finished'] = True
            elif action == 'cancel':
                if not session['pending']:
                    raise TrainingError('没有待成交委托')
                command = {**command, 'alias': session['pending']['alias']}
                self._event(session, command)
                previous = next(e for e in session['events'] if e['id'] == session['pending']['event_id'])
                previous['outcome'] = '已取消'
                session['pending'] = None
            elif action in ('buy', 'sell', 'hold', 'reject', 'note'):
                alias = command.get('alias')
                if alias not in self._visible_aliases(session):
                    raise TrainingError('请选择当前可见候选')
                if action in ('buy', 'sell'):
                    if session['pending']:
                        raise TrainingError('请先推进成交或取消待成交委托')
                    if not self._row(session, alias):
                        raise TrainingError('当前股票无当日行情，不能下单')
                    if action == 'buy' and session['position']:
                        raise TrainingError('首版训练最多持有一只股票，请先卖出持仓')
                    if action == 'sell' and (not session['position'] or session['position']['alias'] != alias):
                        raise TrainingError('该候选没有持仓')
                    if action == 'sell' and session['mode'] == 'close_t' and session['cursor'] <= session['position']['entry_day']:
                        raise TrainingError('买入当日不能卖出，需遵守 T+1')
                    if session['mode'] == 'open_t+1' and session['cursor'] >= len(session['dates']) - 1:
                        raise TrainingError('已到最后一日，没有次日开盘可供成交')
                if action == 'hold' and (not session['position'] or session['position']['alias'] != alias):
                    raise TrainingError('请选择持仓记录持有理由')
                if action == 'reject' and session['position'] and session['position']['alias'] == alias:
                    raise TrainingError('持仓请记录持有或卖出判断')
                event = self._event(session, command)
                if action in ('buy', 'sell'):
                    order = dict(action=action, alias=alias, day=session['cursor'], event_id=event['id'])
                    if session['mode'] == 'close_t':
                        self._fill(session, order, session['cursor'], 'close')
                    else:
                        session['pending'] = order
                        event['outcome'] = '等待次日开盘'
                elif action == 'reject' and alias not in session['rejected']:
                    session['rejected'].append(alias)
            else:
                raise TrainingError('不支持的训练操作')
            session['revision'] += 1
            point = dict(day=session['cursor'], value=self._equity(session))
            if session['equity'][-1]['day'] == point['day']:
                session['equity'][-1] = point
            else:
                session['equity'].append(point)
            self.accounts.record(db, session)
            if simple_step:
                self.store.write_step(db, session)
            else:
                db.execute('UPDATE sessions SET payload=? WHERE id=?', (_dump(session), session_id))
            db.execute('INSERT INTO commands VALUES (?,?)', (session_id, request_id))
        return self._command_view(session, command, simple_step)

    def summary(self):
        finished = self.store.list_for_display(summary=True)
        groups = []
        grouped = {}
        for session in finished:
            key = (self._record_group(session)['id'], session['mode'],
                   session.get('selection', {}).get('type', 'strategy'), session.get('practice_kind', 'screening'), bool(session.get('replay_of')))
            grouped.setdefault(key, []).append(session)
        for (_, mode, selection_type, practice, is_replay), sessions in sorted(grouped.items()):
            trades = [t for s in sessions for t in s['trades']]
            tag_counts = Counter(tag for s in sessions for e in s['events'] if e['action'] == 'buy' for tag in e['tags'])
            groups.append(dict(record_group=self._record_group(sessions[0]), mode=mode, selection_type=selection_type, selection_name=selection_name(selection_type),
                               practice_kind=practice, is_replay=is_replay, sessions=len(sessions), trades=len(trades),
                               win_rate=sum(t['pnl'] > 0 for t in trades) / len(trades) if trades else None,
                               mean_trade_return=sum(t['pnl'] for t in trades) / len(trades) if trades else None,
                               open_positions=sum(s['position'] is not None and not s.get('account_settlement') for s in sessions),
                               settled_positions=sum(bool(s.get('account_settlement')) for s in sessions),
                               buy_tags=dict(tag_counts)))
        return dict(groups=groups, note='按策略、训练方式、选股类型与成交口径分组；首次训练与已看结果的重训单独统计，未平仓不计胜率。标签频次描述习惯，不能证明因果或策略有效。')
