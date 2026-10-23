"""Continuous practice ledger in the existing training SQLite transaction."""
# ruff: noqa: RUF001 -- Chinese product text is intentional.
from __future__ import annotations

import json
import uuid
from collections import defaultdict
from datetime import datetime

from app.services.training import TrainingError, _dump


class TrainingAccounts:
    def __init__(self, trainer):
        self.trainer = trainer
        self.store = trainer.store
        with self.store.connect() as db:
            db.execute('CREATE TABLE IF NOT EXISTS training_accounts '
                       '(id TEXT PRIMARY KEY, verification INTEGER NOT NULL, status TEXT NOT NULL, payload TEXT NOT NULL)')
            db.execute("CREATE UNIQUE INDEX IF NOT EXISTS one_active_training_account "
                       "ON training_accounts(verification) WHERE status='active'")
            db.execute('CREATE TABLE IF NOT EXISTS training_account_sessions '
                       '(session_id TEXT PRIMARY KEY, account_id TEXT NOT NULL, question INTEGER NOT NULL)')
            db.execute('CREATE INDEX IF NOT EXISTS training_account_members '
                       'ON training_account_sessions(account_id, question)')
            db.execute('CREATE TABLE IF NOT EXISTS training_account_points '
                       '(id INTEGER PRIMARY KEY AUTOINCREMENT, account_id TEXT NOT NULL, session_id TEXT NOT NULL, '
                       'question INTEGER NOT NULL, day INTEGER NOT NULL, value REAL NOT NULL, '
                       'UNIQUE(account_id,session_id,day))')

    @staticmethod
    def _read(db, account_id):
        row = db.execute('SELECT payload FROM training_accounts WHERE id=?', (account_id,)).fetchone()
        if row is None:
            raise TrainingError('账户记录不存在', 404)
        return json.loads(row[0])

    @staticmethod
    def _save(db, account):
        db.execute('UPDATE training_accounts SET status=?,payload=? WHERE id=?',
                   (account['status'], _dump(account), account['id']))

    def guard(self, db, session):
        if not session.get('account_id'):
            return None
        account = self._read(db, session['account_id'])
        if account['status'] != 'active':
            raise TrainingError('账户记录已结束，请从开始新训练建立新记录', 409)
        if account['current_session_id'] != session['id']:
            raise TrainingError('账户已有其他当前题，请继续账户当前题', 409)
        return account

    def attach(self, db, session, *, previous_id=None, expected_account_id=None, adopt=False):
        """Only committed activation binds capital; staged questions never call this."""
        if session.get('account_id'):
            return self.guard(db, session)
        if expected_account_id:
            expected = self._read(db, expected_account_id)
            if expected['status'] != 'active':
                raise TrainingError('账户记录已结束，原抽题任务不能重新开始账户', 409)
            if expected['verification'] != session.get('verification', False):
                raise TrainingError('账户与训练类型不一致', 409)
        if previous_id:
            previous = self.store.read(db, previous_id)
            if previous.get('account_id'):
                previous_account = self._read(db, previous['account_id'])
                if previous_account['status'] != 'active':
                    raise TrainingError('账户记录已结束，请从开始新训练建立新记录', 409)
                if previous_account['current_session_id'] != previous_id:
                    raise TrainingError('账户当前题已变化，请刷新后继续', 409)
        row = db.execute("SELECT payload FROM training_accounts WHERE verification=? AND status='active'",
                         (int(session.get('verification', False)),)).fetchone()
        if row:
            account = json.loads(row[0])
            current = self.store.read(db, account['current_session_id'])
            if not current['finished']:
                raise TrainingError('请先结束账户当前题，再切换下一题', 409)
            if adopt:
                raise TrainingError('已有持续账户，请继续账户当前题；旧独立记录保留', 409)
            if account['equity'] <= 0:
                raise TrainingError('账户资金已耗尽，记录保留；可主动结束记录后开始新账户', 409)
            session.update(initial=account['equity'], cash=account['equity'],
                           equity=[dict(day=0, value=account['equity'])])
        else:
            if not adopt:
                session.update(initial=100000.0, cash=100000.0,
                               equity=[dict(day=0, value=100000.0)])
            account = dict(id=uuid.uuid4().hex, verification=session.get('verification', False),
                           status='active', revision=0, initial=session['initial'], equity=session['initial'],
                           current_session_id=None, questions=0, created_at=datetime.now().isoformat(timespec='seconds'),
                           ended_at=None)
            db.execute('INSERT INTO training_accounts VALUES (?,?,?,?)',
                       (account['id'], int(account['verification']), 'active', _dump(account)))
            db.execute('INSERT INTO training_account_points(account_id,session_id,question,day,value) VALUES (?,?,?,?,?)',
                       (account['id'], '', 0, 0, account['initial']))
        account['questions'] += 1
        account['current_session_id'] = session['id']
        session.update(account_id=account['id'], account_question=account['questions'])
        db.execute('INSERT INTO training_account_sessions VALUES (?,?,?)',
                   (session['id'], account['id'], account['questions']))
        self._save(db, account)
        for point in session['equity']:
            self._point(db, session, point)
        self.record(db, session)
        return account

    @staticmethod
    def _point(db, session, point):
        db.execute('INSERT INTO training_account_points(account_id,session_id,question,day,value) VALUES (?,?,?,?,?) '
                   'ON CONFLICT(account_id,session_id,day) DO UPDATE SET value=excluded.value',
                   (session['account_id'], session['id'], session['account_question'], point['day'], point['value']))

    def record(self, db, session):
        if not session.get('account_id'):
            return
        account = self.guard(db, session)
        value = self.trainer._equity(session)
        if session['finished'] and session['position'] and not session.get('account_settlement'):
            session['account_settlement'] = dict(kind='mark_to_market', alias=session['position']['alias'],
                                                 equity=value, day=session['cursor'])
            session['notices'].append('本题持仓按已揭示市值结算至持续账户；不是主动卖出，不计平仓胜率，不另收卖出费用。')
        account.update(equity=value, revision=account['revision'] + 1)
        self._save(db, account)
        self._point(db, session, dict(day=session['cursor'], value=value))

    def session_snapshot(self, session):
        if not session.get('account_id'):
            return None
        with self.store.connect(write=False) as db:
            account = self._read(db, session['account_id'])
            point = db.execute('SELECT id,question,day,value FROM training_account_points '
                               'WHERE account_id=? ORDER BY id DESC LIMIT 1', (account['id'],)).fetchone()
        return {**account, 'total_return': account['equity'] / account['initial'] - 1,
                'point': dict(zip(('id', 'question', 'day', 'value'), point, strict=True)) if point else None}

    def view(self, account_id=None, verification=False):
        with self.store.connect(write=False) as db:
            accounts = [json.loads(row[0]) for row in db.execute(
                'SELECT payload FROM training_accounts WHERE verification=? ORDER BY rowid DESC', (int(verification),))]
            selected = next((a for a in accounts if a['id'] == account_id), None) if account_id else next(
                (a for a in accounts if a['status'] == 'active'), accounts[0] if accounts else None)
            if account_id and not selected:
                raise TrainingError('账户记录不存在', 404)
            points = [dict(zip(('id', 'question', 'day', 'value'), row, strict=True)) for row in db.execute(
                'SELECT id,question,day,value FROM training_account_points WHERE account_id=? ORDER BY id',
                (selected['id'],))] if selected else []
        def summary(account):
            return {**account, 'total_return': account['equity'] / account['initial'] - 1}
        return dict(account=summary(selected) if selected else None, points=points,
                    accounts=[summary(a) for a in accounts])

    def records(self, account_id, limit=200):
        with self.store.connect(write=False) as db:
            self._read(db, account_id)
            rows = db.execute(
                "SELECT m.question,s.id,json_extract(s.payload,'$.trades'), "
                "(SELECT json_group_array(json_remove(value,'$.snapshot')) FROM json_each(s.payload,'$.events')), "
                "json_extract(s.payload,'$.replay_of'),json_extract(s.payload,'$.account_settlement'), "
                "json_extract(s.payload,'$.cursor'),json_extract(s.payload,'$.finished'), "
                "json_extract(s.payload,'$.initial'),json_extract(s.payload,'$.equity[#-1].value'), "
                "json_extract(s.payload,'$.strategies'),json_extract(s.payload,'$.selection'), "
                "json_extract(s.payload,'$.mode') "
                'FROM training_account_sessions m JOIN sessions s ON s.id=m.session_id '
                'WHERE m.account_id=? ORDER BY m.question', (account_id,)).fetchall()
        events, trades, settlements, questions = [], [], [], []
        grouped = defaultdict(list)
        for (question, sid, raw_trades, raw_events, replay, raw_settlement, cursor, finished,
             initial, equity, raw_strategies, raw_selection, mode) in rows:
            strategies = json.loads(raw_strategies or '[]')
            selection = json.loads(raw_selection or '{}')
            equity = equity if equity is not None else initial
            questions.append(dict(id=sid, question=question, cursor=cursor, finished=bool(finished),
                                  initial=initial, equity=equity, total_return=equity / initial - 1 if initial else 0,
                                  strategy='、'.join(s['name'] for s in strategies) or selection.get('name', '随机行情'),
                                  mode=mode, is_replay=bool(replay)))
            question_events = json.loads(raw_events)
            patterns = {event['id']: event.get('pattern', '') for event in question_events}
            for event in question_events:
                events.append({k: event.get(k) for k in ('id', 'day', 'action', 'alias', 'reason', 'tags',
                                                        'invalidation', 'confidence', 'size', 'outcome', 'pattern')}
                              | dict(session_id=sid, question=question, is_replay=bool(replay),
                                     pattern=event.get('pattern') or ''))
            for trade in json.loads(raw_trades):
                item = {**trade, 'pattern': patterns.get(trade['entry_event']) or '未标注模式',
                        'question': question, 'session_id': sid, 'is_replay': bool(replay)}
                trades.append(item)
                grouped[(item['pattern'], bool(replay))].append(item)
            if raw_settlement:
                settlements.append({**json.loads(raw_settlement), 'question': question, 'session_id': sid})
        groups = [dict(pattern=pattern, is_replay=replay, trades=len(items),
                       win_rate=sum(t['pnl'] > 0 for t in items) / len(items),
                       profit=sum(t['profit'] for t in items)) for (pattern, replay), items in sorted(grouped.items())]
        return dict(questions=questions, events=events[-limit:] if limit else events, total_events=len(events),
                    omitted_events=max(0, len(events) - limit) if limit else 0, groups=groups,
                    trades=trades, settlements=settlements,
                    stats=dict(trades=len(trades), win_rate=sum(t['pnl'] > 0 for t in trades) / len(trades) if trades else None,
                               replay_trades=sum(bool(t['is_replay']) for t in trades), settlements=len(settlements)))

    def stop(self, account_id, command):
        namespace = 'account:' + account_id
        with self.store.connect() as db:
            account = self._read(db, account_id)
            repeated = db.execute('SELECT 1 FROM commands WHERE session_id=? AND request_id=?',
                                  (namespace, command['request_id'])).fetchone()
            if not repeated:
                if account['status'] != 'active':
                    raise TrainingError('账户记录已结束', 409)
                if account['revision'] != command['revision']:
                    raise TrainingError('账户版本已变化，请刷新后重试', 409)
                session = self.store.read(db, account['current_session_id'])
                if not session['finished']:
                    if session['pending']:
                        event = next(e for e in session['events'] if e['id'] == session['pending']['event_id'])
                        event['outcome'] = '结束记录时取消，未成交'
                    session.update(pending=None, finished=True, revision=session['revision'] + 1)
                    self.record(db, session)
                    db.execute('UPDATE sessions SET payload=? WHERE id=?', (_dump(session), session['id']))
                    account = self._read(db, account_id)
                account.update(status='closed', revision=account['revision'] + 1,
                               ended_at=datetime.now().isoformat(timespec='seconds'))
                self._save(db, account)
                db.execute('INSERT INTO commands VALUES (?,?)', (namespace, command['request_id']))
        return self.view(account_id, verification=account['verification'])

    def export(self, account_id):
        with self.store.connect(write=False) as db:
            account = self._read(db, account_id)
        return {**self.view(account_id, verification=account['verification']),
                'records': self.records(account_id, limit=None)}
