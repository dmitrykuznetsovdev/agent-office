import datetime
import json
import os
import sys
import tempfile
import time
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import server  # noqa: E402


def line(**entry):
    return json.dumps(entry, ensure_ascii=False) + '\n'


def local_day(offset):
    """Локальная полночь дня offset (0 — сегодня, -1 — вчера), секунды."""
    d = datetime.date.today() + datetime.timedelta(days=offset)
    return time.mktime(d.timetuple())


def iso(seconds):
    return datetime.datetime.utcfromtimestamp(seconds).strftime('%Y-%m-%dT%H:%M:%SZ')


def assistant(mid, tools=(), stop='tool_use', usage=None, ts='2026-01-01T10:00:00Z'):
    content = [{'type': 'tool_use', 'name': name, 'input': inp} for name, inp in tools]
    return line(type='assistant', timestamp=ts, cwd='/work/app', message={
        'id': mid, 'model': 'claude-sonnet-5-5', 'stop_reason': stop, 'content': content,
        'usage': usage or {'input_tokens': 1000, 'output_tokens': 100},
    })


class DescribeTest(unittest.TestCase):
    def test_files_and_commands_read_as_actions(self):
        self.assertEqual(server.describe('Read', {'file_path': '/a/b/cuts.json'}), 'читает cuts.json')
        self.assertEqual(server.describe('Bash', {'command': 'cd /x && python3 tools/run.py build v2'}), '$ run.py build')
        self.assertEqual(server.describe('Bash', {'command': 'ls', 'description': 'Список файлов'}), 'Список файлов')
        self.assertEqual(server.describe('Bash', {'command': 'python3 - <<EOF'}), 'считает скриптом')
        self.assertEqual(server.describe('Bash', {'command': 'until [ -f a ]; do sleep 1; done'}), 'ждёт')

    def test_unknown_mcp_tool_shows_its_name(self):
        self.assertEqual(server.describe('mcp__srv__do_thing', {}), 'srv do_thing')


class CostTest(unittest.TestCase):
    def test_price_per_million_with_cache(self):
        usage = {'input_tokens': 1_000_000, 'output_tokens': 1_000_000, 'cache_read_input_tokens': 1_000_000}
        self.assertAlmostEqual(server.message_cost('claude-sonnet-5-5', usage), 2.0 + 10.0 + 0.2)

    def test_unknown_model_is_free(self):
        self.assertEqual(server.message_cost('other-model', {'input_tokens': 10}), 0.0)


class TailTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 's.jsonl')

    def tearDown(self):
        self.dir.cleanup()

    def write(self, text, mode='w'):
        with open(self.path, mode) as f:
            f.write(text)

    def test_roster_title_cost_and_activity(self):
        self.write(
            line(type='custom-title', customTitle='Ремонт сборки')
            + line(type='attachment', attachment={
                'type': 'agent_listing_delta',
                'addedTypes': ['Explore', 'kit:reviewer'],
                'addedLines': ['- Explore: Search agent', '- kit:reviewer: Reviews diffs'],
            })
            + line(type='user', timestamp='2026-01-01T10:00:00Z', cwd='/work/app/.claude/worktrees/x',
                   message={'role': 'user', 'content': 'почини сборку'})
            # a reply logged in two pieces: paid once
            + assistant('m1', [('Read', {'file_path': '/work/app/Makefile'})])
            + assistant('m1', [('Grep', {'pattern': 'build'})])
        )
        t = server.Tail(self.path).feed()
        self.assertEqual(t.title, 'Ремонт сборки')
        self.assertEqual(t.home, '/work/app')
        self.assertEqual(t.roster, {'Explore': 'Search agent', 'kit:reviewer': 'Reviews diffs'})
        self.assertEqual(t.calls, 2)
        self.assertEqual(t.activity, 'ищет build')
        self.assertTrue(t.busy)
        self.assertAlmostEqual(t.cost, server.message_cost('claude-sonnet-5-5', {'input_tokens': 1000, 'output_tokens': 100}))

    def test_reads_only_what_was_appended(self):
        self.write(assistant('m1', [('Read', {'file_path': 'a'})]))
        t = server.Tail(self.path).feed()
        self.write(assistant('m2', stop='end_turn'), mode='a')
        t.feed()
        self.assertEqual(t.calls, 1)
        self.assertFalse(t.busy)

    def test_cost_is_split_by_local_day(self):
        self.write(assistant('m1', ts=iso(local_day(-1) + 3600)) + assistant('m2', ts=iso(local_day(0) + 3600)))
        t = server.Tail(self.path).feed()
        one = server.message_cost('claude-sonnet-5-5', {'input_tokens': 1000, 'output_tokens': 100})
        self.assertAlmostEqual(t.daily[server.day_of(local_day(0) * 1000)], one)
        self.assertAlmostEqual(server.today_cost(t), one)
        self.assertAlmostEqual(t.cost, 2 * one)

    def test_model_and_tool_kind_follow_the_last_reply(self):
        self.write(assistant('m1', [('Edit', {'file_path': '/a/cuts.json'})]))
        t = server.Tail(self.path).feed()
        self.assertEqual(t.model, 'claude-sonnet-5-5')
        self.assertEqual(t.tool, 'edit')
        self.assertEqual(t.activity, 'правит cuts.json')
        self.write(line(type='assistant', timestamp='2026-01-01T10:01:00Z', message={
            'id': 'm2', 'model': 'claude-haiku-5-5', 'stop_reason': 'tool_use',
            'content': [{'type': 'tool_use', 'name': 'Bash', 'input': {'command': 'npm test'}}],
            'usage': {'input_tokens': 1, 'output_tokens': 1}}), mode='a')
        t.feed()
        self.assertEqual(t.model, 'claude-haiku-5-5')
        self.assertEqual(t.tool, 'bash')

    def test_tool_kinds(self):
        kinds = {name: server.tool_kind(name) for name in
                 ['Read', 'Write', 'Edit', 'MultiEdit', 'Grep', 'Glob', 'Bash', 'WebFetch', 'Agent', 'Task', 'Skill', 'mcp__x__y']}
        self.assertEqual(kinds, {'Read': 'read', 'Write': 'write', 'Edit': 'edit', 'MultiEdit': 'edit', 'Grep': 'grep',
                                 'Glob': 'grep', 'Bash': 'bash', 'WebFetch': 'web', 'Agent': 'agent', 'Task': 'agent',
                                 'Skill': 'skill', 'mcp__x__y': 'other'})

    def test_removed_agents_leave_the_roster(self):
        self.write(line(type='attachment', attachment={'type': 'agent_listing_delta', 'addedTypes': ['a', 'b']})
                   + line(type='attachment', attachment={'type': 'agent_listing_delta', 'removedTypes': ['a']}))
        self.assertEqual(list(server.Tail(self.path).feed().roster), ['b'])


class SessionsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.projects, server.PROJECTS = server.PROJECTS, self.dir.name
        self.codex, server.CODEX_SESSIONS = server.CODEX_SESSIONS, os.path.join(self.dir.name, 'нет-codex')
        server.TAILS.clear()

    def tearDown(self):
        server.PROJECTS = self.projects
        server.CODEX_SESSIONS = self.codex
        server.TAILS.clear()
        self.dir.cleanup()

    def test_sessions_report_today(self):
        pdir = os.path.join(self.dir.name, '-work-app')
        sub = os.path.join(pdir, 'sid1', 'subagents')
        os.makedirs(sub)
        yesterday, today = iso(local_day(-1) + 3600), iso(local_day(0) + 3600)
        with open(os.path.join(pdir, 'sid1.jsonl'), 'w') as f:
            f.write(assistant('m1', ts=yesterday) + assistant('m2', ts=today))
        with open(os.path.join(sub, 'agent-a1.jsonl'), 'w') as f:
            f.write(assistant('a1', ts=yesterday) + assistant('a2', ts=today))
        with open(os.path.join(sub, 'agent-a1.meta.json'), 'w') as f:
            json.dump({'agentType': 'Explore', 'description': 'ищет', 'finished': True}, f)
        one = server.message_cost('claude-sonnet-5-5', {'input_tokens': 1000, 'output_tokens': 100})
        [s] = server.sessions()
        self.assertEqual(s['id'], 'sid1')
        self.assertAlmostEqual(s['today'], round(2 * one, 2))
        self.assertAlmostEqual(s['cost'], round(4 * one, 2))


    def test_agents_and_producer_carry_model_tool_and_finish(self):
        pdir = os.path.join(self.dir.name, '-work-app')
        sub = os.path.join(pdir, 'sid1', 'subagents')
        os.makedirs(sub)
        with open(os.path.join(pdir, 'sid1.jsonl'), 'w') as f:
            f.write(assistant('m1', [('Agent', {'subagent_type': 'Explore'})]))
        with open(os.path.join(sub, 'agent-a1.jsonl'), 'w') as f:
            f.write(line(type='assistant', timestamp='2026-01-01T10:00:05Z', message={
                'id': 'a1', 'model': 'claude-haiku-5-5', 'stop_reason': 'tool_use',
                'content': [{'type': 'tool_use', 'name': 'Grep', 'input': {'pattern': 'TODO'}}],
                'usage': {'input_tokens': 1, 'output_tokens': 1}}))
        with open(os.path.join(sub, 'agent-a1.meta.json'), 'w') as f:
            json.dump({'agentType': 'Explore', 'description': 'ищет', 'finished': True}, f)
        st = server.state('sid1')
        [a] = st['agents']
        self.assertEqual((a['model'], a['tool'], a['finished']), ('claude-haiku-5-5', 'grep', True))
        self.assertEqual(a['at'], server.ms('2026-01-01T10:00:05Z'))
        self.assertEqual((st['producer']['model'], st['producer']['tool']), ('claude-sonnet-5-5', 'agent'))


class LedgerTest(unittest.TestCase):
    one = {'input_tokens': 1000, 'output_tokens': 100}

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.pdir = os.path.join(self.dir.name, '-work-app')
        os.makedirs(os.path.join(self.pdir, 'sid1', 'subagents'))
        self.main = os.path.join(self.pdir, 'sid1.jsonl')
        self.agent = os.path.join(self.pdir, 'sid1', 'subagents', 'agent-a1.jsonl')
        self.cache = os.path.join(self.dir.name, 'stats.json')
        self.usd = server.message_cost('claude-sonnet-5-5', self.one)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, path, text, mode='w'):
        with open(path, mode) as f:
            f.write(text)

    def day(self, offset):
        return server.day_of(local_day(offset) * 1000)

    def test_parts_add_up_to_message_cost(self):
        usage = {'input_tokens': 10, 'output_tokens': 20, 'cache_read_input_tokens': 30,
                 'cache_creation': {'ephemeral_5m_input_tokens': 40, 'ephemeral_1h_input_tokens': 50}, 'speed': 'fast'}
        self.assertAlmostEqual(sum(server.cost_parts('claude-opus-5-5', usage)), server.message_cost('claude-opus-5-5', usage))

    def test_days_split_by_model_project_and_session(self):
        self.write(self.main, assistant('m1', ts=iso(local_day(-1) + 3600)) + assistant('m1', ts=iso(local_day(-1) + 3600))
                   + assistant('m2', ts=iso(local_day(0) + 3600)))
        self.write(self.agent, assistant('a1', ts=iso(local_day(0) + 7200)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        days = {d['day']: d for d in ledger.report()['days']}
        self.assertAlmostEqual(days[self.day(-1)]['usd'], self.usd)  # m1 logged twice, paid once
        today = days[self.day(0)]
        self.assertAlmostEqual(today['usd'], 2 * self.usd)
        self.assertAlmostEqual(today['models']['claude-sonnet-5-5'], 2 * self.usd)
        self.assertAlmostEqual(today['projects']['app'], 2 * self.usd)
        self.assertEqual(today['sessions'], 1)  # subagent is not a session of its own
        self.assertEqual((days[self.day(-1)]['started'], today['started']), (1, 0))  # started yesterday
        self.assertEqual(today['tokens'], 2200)

    def test_tokens_split_by_kind_model_and_project(self):
        usage = {'input_tokens': 10, 'output_tokens': 20, 'cache_read_input_tokens': 30,
                 'cache_creation': {'ephemeral_5m_input_tokens': 40, 'ephemeral_1h_input_tokens': 50}}
        self.write(self.main, assistant('m1', usage=usage, ts=iso(local_day(0) + 3600)))
        self.write(self.agent, assistant('a1', ts=iso(local_day(0) + 7200)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        today = ledger.report()['days'][-1]
        self.assertEqual(today['tokParts'], [1010, 120, 30, 90])  # input, output, cache read, cache write
        self.assertEqual(today['tokModels'], {'claude-sonnet-5-5': 1250})
        self.assertEqual(today['tokProjects'], {'app': 1250})
        self.assertEqual(today['tokens'], 1250)

    def old_cache(self, journal_offset):
        """stats.json before tokens were split: cells end with the total token count."""
        hour = time.strftime('%Y-%m-%dT%H', time.localtime(local_day(-1) + 3600))
        self.write(self.cache, json.dumps({
            'files': {self.main: {'offset': journal_offset, 'ids': ['m1'], 'days': [hour[:10]], 'main': True}},
            'hours': {hour: {'app': {'claude-sonnet-5-5': [self.usd, 0.0, 0.0, 0.0, 1100]}}},
        }))

    def test_old_cache_is_rebuilt_while_its_journals_are_on_disk(self):
        text = assistant('m1', ts=iso(local_day(-1) + 3600))
        self.write(self.main, text)
        self.old_cache(len(text.encode()))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        day = ledger.report()['days'][0]
        self.assertAlmostEqual(day['usd'], self.usd)  # read again, not counted twice
        self.assertEqual(day['tokParts'], [1000, 100, 0, 0])

    def test_old_cache_keeps_totals_when_journals_are_gone(self):
        self.old_cache(500)
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        day = ledger.report()['days'][0]
        self.assertAlmostEqual(day['usd'], self.usd)
        self.assertEqual((day['tokens'], day['tokParts']), (1100, [0, 0, 0, 0]))  # no split for that history
        # a new journal writing into the same hour adds to the old cell
        self.write(os.path.join(self.pdir, 'sid2.jsonl'), assistant('m2', ts=iso(local_day(-1) + 3600)))
        ledger.scan(self.dir.name)
        day = ledger.report()['days'][0]
        self.assertEqual((day['tokens'], day['tokParts']), (2200, [1000, 100, 0, 0]))

    def test_report_fills_quiet_days_and_rolls_24h(self):
        self.write(self.main, assistant('m1', ts=iso(local_day(-3) + 3600)) + assistant('m2', ts=iso(time.time() - 600)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        report = ledger.report()
        self.assertEqual(report['since'], self.day(-3))
        self.assertEqual([d['day'] for d in report['days']], [self.day(i) for i in (-3, -2, -1, 0)])
        self.assertEqual(report['days'][1]['usd'], 0)
        self.assertAlmostEqual(report['last24'], self.usd)

    def test_reads_only_appended_lines(self):
        self.write(self.main, assistant('m1', ts=iso(local_day(0) + 3600)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        self.write(self.main, assistant('m2', ts=iso(local_day(0) + 3600)), mode='a')
        ledger.scan(self.dir.name)
        ledger.scan(self.dir.name)
        self.assertAlmostEqual(ledger.report()['days'][-1]['usd'], 2 * self.usd)

    def test_history_outlives_deleted_journals(self):
        self.write(self.main, assistant('m1', ts=iso(local_day(-1) + 3600)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        ledger.save()
        os.remove(self.main)
        again = server.Ledger(self.cache)
        again.scan(self.dir.name)
        self.assertAlmostEqual(again.report()['days'][0]['usd'], self.usd)

    def test_reloaded_cache_does_not_count_twice(self):
        self.write(self.main, assistant('m1', ts=iso(local_day(0) + 3600)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        ledger.save()
        again = server.Ledger(self.cache)
        again.scan(self.dir.name)
        self.assertAlmostEqual(again.report()['days'][-1]['usd'], self.usd)

    def test_hours_for_heatmap(self):
        at = local_day(0) + 14 * 3600 + 60
        self.write(self.main, assistant('m1', ts=iso(at)))
        ledger = server.Ledger(self.cache)
        ledger.scan(self.dir.name)
        [[day, hour, usd]] = ledger.report()['hours']
        self.assertEqual((day, hour), (self.day(0), 14))
        self.assertAlmostEqual(usd, self.usd)


THREAD = '01a18b44-21b2-7481-961a-0f2ee2ca8ea2'


def rec(kind, payload, ts='2026-01-01T10:00:00.000Z'):
    return line(type=kind, timestamp=ts, payload=payload)


def meta(thread=THREAD, cwd='/work/app', source='vscode', **more):
    return rec('session_meta', dict(id=thread, session_id=thread, cwd=cwd, source=source, originator='Codex Desktop', **more))


def context(model='gpt-5.6-sol', cwd='/work/app', ts='2026-01-01T10:00:00.000Z'):
    return rec('turn_context', {'model': model, 'cwd': cwd}, ts)


def event(kind, ts='2026-01-01T10:00:00.000Z', **more):
    return rec('event_msg', dict(type=kind, **more), ts)


def usage_record(total_in=1000, cached=800, out=100, ts='2026-01-01T10:00:01.000Z'):
    return rec('token_usage_record', {'response_id': 'r', 'usage': {
        'input_tokens': total_in, 'cached_input_tokens': cached, 'cache_write_input_tokens': 0,
        'output_tokens': out, 'reasoning_output_tokens': 0, 'total_tokens': total_in + out}}, ts)


def token_count(total_in, cached, out, ts='2026-01-01T10:00:02.000Z', limits=None):
    return event('token_count', ts, rate_limits=limits, info={'total_token_usage': {
        'input_tokens': total_in, 'cached_input_tokens': cached, 'output_tokens': out, 'total_tokens': total_in + out}})


def item(kind, ts='2026-01-01T10:00:03.000Z', **more):
    return event('item_completed', ts, item=dict(type=kind, **more))


USAGE = {'input_tokens': 200, 'output_tokens': 100, 'cache_read_input_tokens': 800, 'cache_creation_input_tokens': 0}


class CodexUsageTest(unittest.TestCase):
    def fold(self, text):
        meter, out = {}, []
        for raw in text.splitlines():
            u = server.codex_usage(meter, json.loads(raw))
            if u:
                out.append(u)
        return out

    def test_cached_tokens_leave_the_input(self):
        self.assertEqual(self.fold(context() + usage_record()), [USAGE])

    def test_price_is_per_million_of_uncached_input(self):
        usd = server.message_cost('gpt-5.6-sol', {'input_tokens': 1_000_000, 'output_tokens': 1_000_000, 'cache_read_input_tokens': 1_000_000})
        self.assertAlmostEqual(usd, 4.0 + 20.0 + 0.4)

    def test_old_logs_count_by_growth_of_the_running_total(self):
        got = self.fold(context() + token_count(1000, 800, 100) + token_count(1000, 800, 100) + token_count(1500, 1000, 150))
        self.assertEqual(got, [USAGE, {'input_tokens': 300, 'output_tokens': 50, 'cache_read_input_tokens': 200, 'cache_creation_input_tokens': 0}])

    def test_records_win_over_the_running_total(self):
        got = self.fold(context() + usage_record() + token_count(1000, 800, 100) + usage_record() + token_count(2000, 1600, 200))
        self.assertEqual(got, [USAGE, USAGE])

    def test_total_going_back_after_a_rollback_is_not_new_spending(self):
        got = self.fold(context() + token_count(5000, 0, 500) + token_count(3000, 0, 300) + token_count(3100, 0, 310))
        self.assertEqual([u['input_tokens'] for u in got], [5000, 100])

    def test_fast_tier_is_kept(self):
        got = self.fold(context() + event('thread_settings_applied', thread_settings={'service_tier': 'fast'}) + usage_record())
        self.assertEqual(got[0]['speed'], 'fast')

    def test_model_known_before_the_first_turn(self):
        meter = {}
        settings = event('thread_settings_applied', thread_settings={'model': 'gpt-5.5'})
        for raw in (settings + token_count(10, 0, 1)).splitlines():
            server.codex_usage(meter, json.loads(raw))
        self.assertEqual(server.codex_model(meter), 'gpt-5.5')

    def test_review_thread_after_compaction_is_still_the_review_model(self):
        meter = {}
        text = meta(thread='child', source={'subagent': {'other': 'guardian'}}) + token_count(10, 0, 1)
        for raw in text.splitlines():
            server.codex_usage(meter, json.loads(raw))
        self.assertEqual(server.codex_model(meter), 'codex-auto-review')

    def test_provider_by_model(self):
        self.assertEqual([server.provider_of(m) for m in ('claude-opus-5-5', 'gpt-6-astra', 'codex-auto-review', 'gpt-?')],
                         ['claude', 'codex', 'codex', 'codex'])


class CodexTailTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.path = os.path.join(self.dir.name, 'rollout-2026-01-01T10-00-00-%s.jsonl' % THREAD)

    def tearDown(self):
        self.dir.cleanup()

    def tail(self, text):
        with open(self.path, 'w') as f:
            f.write(text)
        return server.CodexTail(self.path).feed()

    def test_thread_folder_prompt_and_cost(self):
        t = self.tail(meta(cwd='/work/app') + context() + item('UserMessage', content=[{'type': 'text', 'text': 'почини сборку'}]) + usage_record())
        self.assertEqual((t.thread, t.home, t.prompt, t.model), (THREAD, '/work/app', 'почини сборку', 'gpt-5.6-sol'))
        self.assertAlmostEqual(t.cost, server.message_cost('gpt-5.6-sol', USAGE))

    def test_turn_marks_busy_until_it_ends(self):
        self.assertTrue(self.tail(meta() + event('task_started')).busy)
        self.assertFalse(self.tail(meta() + event('task_started') + event('task_complete')).busy)
        self.assertFalse(self.tail(meta() + event('task_started') + event('turn_aborted')).busy)

    def test_finished_items_read_as_actions(self):
        t = self.tail(meta()
                      + item('CommandExecution', command=['/bin/zsh', '-lc', 'npm test'], parsed_cmd=[{'type': 'unknown', 'cmd': 'npm test'}])
                      + item('CommandExecution', command=['/bin/zsh', '-lc', 'cat a/b.py'], parsed_cmd=[{'type': 'read', 'cmd': 'cat a/b.py', 'name': 'b.py', 'path': 'a/b.py'}])
                      + item('CommandExecution', command=['/bin/zsh', '-lc', 'rg foo'], parsed_cmd=[{'type': 'search', 'cmd': 'rg foo', 'query': 'foo'}])
                      + item('FileChange', changes={'/work/app/x.ts': {'type': 'update'}}, status='completed')
                      + item('FileChange', changes={'/work/app/new.ts': {'type': 'add'}}, status='completed')
                      + item('WebSearch', query='что-то')
                      + item('McpToolCall', server='notion', tool='search', status='completed')
                      + item('ImageView', path='/tmp/shot.png'))
        self.assertEqual([what for _, what in t.events],
                         ['$ npm test', 'читает b.py', 'ищет foo', 'правит x.ts', 'пишет new.ts', 'гуглит', 'листает Notion', 'читает shot.png'])
        self.assertEqual(t.calls, 8)
        self.assertEqual(t.tool, 'read')

    def test_call_in_progress_is_seen_before_it_finishes(self):
        call = rec('response_item', {'type': 'custom_tool_call', 'name': 'exec',
                                     'input': 'const r = await tools.exec_command({cmd:"npm run build",yield_time_ms:1000});'})
        t = self.tail(meta() + call)
        self.assertEqual((t.activity, t.tool), ('$ npm run', 'bash'))
        self.assertEqual(t.calls, 0)  # посчитается, когда вызов закончится

    def test_review_thread_knows_its_parent(self):
        t = self.tail(meta(thread='child', source={'subagent': {'other': 'guardian'}}, parent_thread_id=THREAD))
        self.assertEqual((t.parent, t.role), (THREAD, 'guardian'))

    def test_spawned_thread_knows_its_parent(self):
        t = self.tail(meta(thread='child', source={'subagent': {'thread_spawn': {'parent_thread_id': THREAD}}}, agent_nickname='Maxwell'))
        self.assertEqual((t.parent, t.role, t.nickname), (THREAD, 'general-purpose', 'Maxwell'))


class CodexSessionsTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.saved = server.PROJECTS, server.CODEX_SESSIONS, server.CODEX_INDEX, server.DATA
        server.PROJECTS = os.path.join(self.dir.name, 'claude')
        server.CODEX_SESSIONS = os.path.join(self.dir.name, 'sessions')
        server.CODEX_INDEX = os.path.join(self.dir.name, 'session_index.jsonl')
        server.DATA = self.dir.name
        os.makedirs(server.PROJECTS)
        os.makedirs(server.CODEX_SESSIONS)
        server.TAILS.clear()

    def tearDown(self):
        server.PROJECTS, server.CODEX_SESSIONS, server.CODEX_INDEX, server.DATA = self.saved
        server.TAILS.clear()
        self.dir.cleanup()

    def write(self, thread, text):
        day = os.path.join(server.CODEX_SESSIONS, '2026', '01', '01')
        os.makedirs(day, exist_ok=True)
        with open(os.path.join(day, 'rollout-2026-01-01T10-00-00-%s.jsonl' % thread), 'w') as f:
            f.write(text)

    def now(self, offset=0):
        return iso(time.time() + offset)

    def test_session_is_listed_with_its_provider_and_title(self):
        self.write(THREAD, meta() + context(ts=self.now()) + event('task_started', self.now()) + usage_record(ts=self.now()))
        with open(server.CODEX_INDEX, 'w') as f:
            f.write(json.dumps({'id': THREAD, 'thread_name': 'Разобрать события'}) + '\n')
        [s] = server.sessions()
        self.assertEqual((s['provider'], s['title'], s['project'], s['id']), ('codex', 'Разобрать события', 'app', THREAD))
        self.assertEqual(s['working'], 1)
        self.assertTrue(s['isLive'])
        self.assertAlmostEqual(s['today'], round(server.message_cost('gpt-5.6-sol', USAGE), 2))

    def test_prompt_is_the_title_until_codex_names_the_thread(self):
        self.write(THREAD, meta() + item('UserMessage', self.now(), content=[{'type': 'text', 'text': 'почини сборку'}]))
        self.assertEqual(server.sessions()[0]['title'], 'почини сборку')

    def test_touched_file_without_new_records_is_not_a_recent_session(self):
        self.write(THREAD, meta() + context(ts=iso(time.time() - 3 * 86400)))
        self.assertEqual(server.sessions(), [])

    def test_review_thread_works_at_its_parents_desk(self):
        self.write(THREAD, meta() + context(ts=self.now()) + usage_record(ts=self.now()))
        self.write('child', meta(thread='child', source={'subagent': {'other': 'guardian'}}, parent_thread_id=THREAD)
                   + context('codex-auto-review', ts=self.now()) + event('task_started', self.now()) + usage_record(ts=self.now()))
        [s] = server.sessions()
        self.assertEqual((s['id'], s['agents'], s['working']), (THREAD, 1, 1))
        st = server.state(THREAD)
        [a] = st['agents']
        self.assertEqual((a['type'], a['model'], a['isWorking']), ('guardian', 'codex-auto-review', True))
        self.assertEqual([d['name'] for z in st['office']['zones'] for d in z['desks']], ['Главный', 'Контролёр'])

    def test_codex_office_has_its_own_sign_and_no_generation(self):
        self.write(THREAD, meta() + context(ts=self.now()) + event('task_started', self.now()))
        st = server.state(THREAD)
        self.assertEqual(st['office']['provider'], 'codex')
        self.assertEqual(st['office']['accent'], server.CODEX_ACCENT)
        self.assertEqual(st['office']['title'], 'ОФИС · APP')
        self.assertTrue(st['producer']['isBusy'])
        self.assertEqual(st['producer']['model'], 'gpt-5.6-sol')

    def test_folder_with_a_claude_office_stays_codex_inside_codex_session(self):
        os.makedirs(os.path.dirname(server.office_path('/work/app')), exist_ok=True)
        with open(server.office_path('/work/app'), 'w') as f:
            json.dump({'folder': '/work/app', 'title': 'СТУДИЯ', 'zones': [], 'guests': {}}, f)
        self.write(THREAD, meta() + context(ts=self.now()))
        self.assertEqual(server.state(THREAD)['office']['title'], 'ОФИС · APP')

    def test_no_codex_folder_is_fine(self):
        server.CODEX_SESSIONS = os.path.join(self.dir.name, 'нет')
        self.assertEqual(server.sessions(), [])


class LedgerCodexTest(unittest.TestCase):
    usd = server.message_cost('gpt-5.6-sol', USAGE)

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.claude = os.path.join(self.dir.name, 'claude')
        self.codex = os.path.join(self.dir.name, 'sessions')
        os.makedirs(os.path.join(self.claude, '-work-app'))
        self.day = os.path.join(self.codex, '2026', '01', '01')
        os.makedirs(self.day)
        self.path = os.path.join(self.day, 'rollout-2026-01-01T10-00-00-%s.jsonl' % THREAD)
        self.cache = os.path.join(self.dir.name, 'stats.json')
        self.at = iso(local_day(0) + 3600)

    def tearDown(self):
        self.dir.cleanup()

    def write(self, text, mode='w', path=None):
        with open(path or self.path, mode) as f:
            f.write(text)

    def scan(self, ledger=None):
        ledger = ledger or server.Ledger(self.cache)
        ledger.scan(self.claude, self.codex)
        return ledger

    def zstd(self):
        exe = server.find_zstd()
        if not exe:
            self.skipTest('нет zstd')
        import subprocess
        subprocess.run([exe, '-q', '--rm', '-f', self.path], check=True)

    def session(self):
        return meta() + context(ts=self.at) + usage_record(ts=self.at)

    def test_codex_spending_lands_in_the_same_report(self):
        self.write(self.session())
        with open(os.path.join(self.claude, '-work-app', 'sid.jsonl'), 'w') as f:
            f.write(assistant('m1', ts=self.at))
        today = self.scan().report()['days'][-1]
        self.assertEqual(set(today['models']), {'gpt-5.6-sol', 'claude-sonnet-5-5'})
        self.assertAlmostEqual(today['projects']['app'], self.usd + server.message_cost('claude-sonnet-5-5', {'input_tokens': 1000, 'output_tokens': 100}), delta=1e-4)  # отчёт округляет до 4 знаков
        self.assertEqual(today['tokParts'][:3], [1200, 200, 800])
        self.assertEqual((today['sessions'], today['started']), (2, 2))

    def test_report_for_one_provider(self):
        self.write(self.session())
        with open(os.path.join(self.claude, '-work-app', 'sid.jsonl'), 'w') as f:
            f.write(assistant('m1', ts=iso(local_day(-2) + 3600)))
        ledger = self.scan()
        codex = ledger.report(provider='codex')
        self.assertEqual(codex['since'], server.day_of(local_day(0) * 1000))
        self.assertEqual(list(codex['days'][-1]['models']), ['gpt-5.6-sol'])
        self.assertEqual((codex['days'][-1]['sessions'], codex['days'][-1]['started']), (1, 1))
        self.assertAlmostEqual(codex['days'][-1]['usd'], self.usd, delta=1e-4)  # отчёт округляет до 4 знаков
        self.assertEqual([h[2] for h in codex['hours']], [round(self.usd, 4)])
        claude = ledger.report(provider='claude')
        self.assertEqual(claude['since'], server.day_of(local_day(-2) * 1000))
        self.assertEqual(claude['days'][-1]['usd'], 0)
        self.assertEqual(set(ledger.report()['first']), {'claude', 'codex'})
        self.assertEqual(ledger.report()['first']['codex'], server.day_of(local_day(0) * 1000))

    def test_review_thread_pays_but_is_not_a_session(self):
        self.write(meta(thread='child', source={'subagent': {'other': 'guardian'}}, parent_thread_id=THREAD)
                   + context('codex-auto-review', ts=self.at) + usage_record(ts=self.at))
        today = self.scan().report()['days'][-1]
        self.assertEqual(today['tokModels'], {'codex-auto-review': 1100})
        self.assertEqual((today['sessions'], today['started']), (0, 0))
        self.assertEqual(today['usd'], 0)  # цены нет в прайсе

    def test_chatgpt_project_folder_gets_a_readable_name(self):
        self.write(meta(cwd='/u/.codex/.chatgpt-projects/g-p-6aa15ef21e70') + context(cwd='/u/.codex/.chatgpt-projects/g-p-6aa15ef21e70', ts=self.at) + usage_record(ts=self.at))
        self.assertEqual(list(self.scan().report()['days'][-1]['projects']), ['ChatGPT-проект'])

    def test_appended_lines_are_read_once(self):
        self.write(self.session())
        ledger = self.scan()
        self.write(usage_record(ts=self.at), mode='a')
        self.scan(ledger)
        self.scan(ledger)
        self.assertAlmostEqual(ledger.report()['days'][-1]['usd'], 2 * self.usd, delta=1e-4)  # отчёт округляет до 4 знаков

    def test_compressed_journal_is_read(self):
        self.write(self.session())
        self.zstd()
        self.assertAlmostEqual(self.scan().report()['days'][-1]['usd'], self.usd, delta=1e-4)  # отчёт округляет до 4 знаков

    def test_compressing_a_journal_does_not_count_it_twice(self):
        self.write(self.session())
        ledger = self.scan()
        ledger.save()
        self.zstd()
        again = server.Ledger(self.cache)
        self.scan(again)
        self.scan(again)
        self.assertAlmostEqual(again.report()['days'][-1]['usd'], self.usd, delta=1e-4)  # отчёт округляет до 4 знаков

    def test_journal_resumed_after_compression_adds_only_the_new_part(self):
        self.write(self.session())
        ledger = self.scan()
        self.zstd()
        self.scan(ledger)
        import subprocess
        subprocess.run([server.find_zstd(), '-q', '-d', '--rm', '-f', self.path + '.zst'], check=True)
        self.write(usage_record(ts=self.at), mode='a')
        self.scan(ledger)
        self.assertAlmostEqual(ledger.report()['days'][-1]['usd'], 2 * self.usd, delta=1e-4)  # отчёт округляет до 4 знаков

    def test_without_zstd_compressed_journals_wait(self):
        self.write(self.session())
        self.zstd()
        find, server.find_zstd = server.find_zstd, lambda: None
        try:
            ledger = self.scan()
            self.assertEqual(ledger.report()['days'], [])
        finally:
            server.find_zstd = find
        self.assertAlmostEqual(self.scan(ledger).report()['days'][-1]['usd'], self.usd, delta=1e-4)  # отчёт округляет до 4 знаков

    def test_old_stats_file_keeps_claude_and_gains_codex(self):
        hour = time.strftime('%Y-%m-%dT%H', time.localtime(local_day(-1) + 3600))
        cell = [1.0, 0.0, 0.0, 0.0, 1100, 1000, 100, 0, 0]
        self.write(json.dumps({'v': server.Ledger.VERSION, 'files': {}, 'hours': {hour: {'app': {'claude-opus-5-5': cell}}}}), path=self.cache)
        self.write(self.session())
        days = {d['day']: d for d in self.scan().report()['days']}
        self.assertEqual(days[hour[:10]]['usd'], 1.0)
        self.assertAlmostEqual(days[server.day_of(local_day(0) * 1000)]['usd'], self.usd, delta=1e-4)  # отчёт округляет до 4 знаков

    def test_latest_limits_survive_a_restart(self):
        old = {'limit_id': 'codex', 'plan_type': 'plus', 'primary': {'used_percent': 90.0, 'window_minutes': 300, 'resets_at': 100}}
        new = {'limit_id': 'codex', 'plan_type': 'plus', 'primary': {'used_percent': 24.0, 'window_minutes': 300, 'resets_at': 200},
               'secondary': {'used_percent': 8.0, 'window_minutes': 10080, 'resets_at': 300}}
        self.write(meta() + context(ts=self.at) + token_count(10, 0, 1, ts='2026-01-02T10:00:00.000Z', limits=new)
                   + token_count(11, 0, 1, ts='2026-01-01T10:00:00.000Z', limits=old))
        ledger = self.scan()
        ledger.save()
        limits = server.Ledger(self.cache).report()['limits']
        self.assertEqual(limits['plan'], 'plus')
        self.assertEqual([(w['minutes'], w['used'], w['resets']) for w in limits['windows']], [(300, 24.0, 200), (10080, 8.0, 300)])


FAKE_OK = '''#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"is_error": False, "structured_output": {
    "title": "Новый", "boss": {"name": "Тимлид", "prop": "crown"},
    "zones": [{"name": "код", "desks": [{"type": "kit:builder", "name": "Сборщик", "prop": "box"}]}], "guests": []}}))
'''
FAKE_LOGGED_OUT = '''#!/usr/bin/env python3
import json, sys
sys.stdin.read()
print(json.dumps({"is_error": True, "result": "Not logged in · Please run /login"}))
sys.exit(1)
'''
FAKE_STUCK = '''#!/usr/bin/env python3
import time
time.sleep(30)
'''


class GenerationTest(unittest.TestCase):
    roster = {'kit:builder': '', 'Explore': ''}
    folder = '/work/app'

    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.data, server.DATA = server.DATA, self.dir.name
        self.find, self.timeout = server.find_claude, server.GEN_TIMEOUT
        server.JOBS.clear()

    def tearDown(self):
        for job in server.JOBS.values():
            if job.get('proc') and job['proc'].poll() is None:
                job['proc'].kill()
        server.DATA, server.find_claude, server.GEN_TIMEOUT = self.data, self.find, self.timeout
        self.dir.cleanup()

    def fake(self, body):
        path = os.path.join(self.dir.name, 'claude')
        with open(path, 'w') as f:
            f.write(body)
        os.chmod(path, 0o755)
        server.find_claude = lambda: path

    def saved(self, title):
        os.makedirs(os.path.dirname(server.office_path(self.folder)), exist_ok=True)
        with open(server.office_path(self.folder), 'w') as f:
            json.dump({'folder': self.folder, 'title': title, 'zones': [], 'guests': {}}, f)

    def wait(self):
        for _ in range(100):
            view = server.job_view(self.folder)
            if view['status'] != 'running':
                return view
            time.sleep(0.05)
        self.fail('generation did not finish')

    def test_generation_runs_in_background_and_keeps_the_old_office_for_undo(self):
        self.fake(FAKE_OK)
        self.saved('СТАРЫЙ')
        server.start_office(self.folder, self.roster)
        self.assertEqual(self.wait()['status'], 'done')
        self.assertEqual(server.load_office(self.folder)['title'], 'НОВЫЙ')
        self.assertTrue(server.job_view(self.folder)['canUndo'])
        self.assertTrue(server.undo_office(self.folder))
        self.assertEqual(server.load_office(self.folder)['title'], 'СТАРЫЙ')
        self.assertFalse(server.job_view(self.folder)['canUndo'])

    def test_failure_names_the_reason_and_leaves_the_office_alone(self):
        self.fake(FAKE_LOGGED_OUT)
        self.saved('СТАРЫЙ')
        server.start_office(self.folder, self.roster)
        view = self.wait()
        self.assertEqual(view['status'], 'failed')
        self.assertIn('Not logged in', view['error'])
        self.assertEqual(server.load_office(self.folder)['title'], 'СТАРЫЙ')

    def test_no_office_is_written_when_generation_fails(self):
        self.fake(FAKE_LOGGED_OUT)
        server.start_office(self.folder, self.roster)
        self.wait()
        self.assertIsNone(server.load_office(self.folder))

    def test_cancel_stops_claude(self):
        self.fake(FAKE_STUCK)
        self.saved('СТАРЫЙ')
        server.start_office(self.folder, self.roster)
        time.sleep(0.3)
        self.assertTrue(server.cancel_office(self.folder))
        self.assertEqual(self.wait()['status'], 'cancelled')
        self.assertEqual(server.load_office(self.folder)['title'], 'СТАРЫЙ')

    def test_timeout_explains_what_to_check(self):
        self.fake(FAKE_STUCK)
        server.GEN_TIMEOUT = 0.5
        server.start_office(self.folder, self.roster)
        view = self.wait()
        self.assertEqual(view['status'], 'failed')
        self.assertIn('/login', view['error'])

    def test_second_start_while_running_is_refused(self):
        self.fake(FAKE_STUCK)
        server.start_office(self.folder, self.roster)
        self.assertIsNone(server.start_office(self.folder, self.roster))


class WatchParentTest(unittest.TestCase):
    def test_parent_alive_false_when_ppid_differs(self):
        self.assertTrue(server.parent_alive(os.getppid()))
        self.assertFalse(server.parent_alive(os.getppid() + 12345))


class OfficeTest(unittest.TestCase):
    roster = {'kit:reviewer': '', 'kit:builder': '', 'solo': '', 'Explore': ''}

    def test_russian_job_titles(self):
        self.assertEqual(server.ru_name('caveman:cavecrew-reviewer'), 'Ревьюер')
        self.assertEqual(server.ru_name('codex:codex-rescue'), 'Спасатель')
        self.assertEqual(server.ru_name('reels-core:motion-designer'), 'Аниматор')
        self.assertEqual(server.ru_name('reels-core:qa-inspector'), 'Контролёр')
        self.assertEqual(server.ru_name('x:art-director'), 'Арт-директор')
        self.assertEqual(server.ru_name('x:layer-inspector'), 'Инспектор')  # head noun when the phrase is unknown
        self.assertEqual(server.ru_name('solo'), 'Solo')  # nothing to translate: the name stays

    def test_auto_office_groups_by_kind_of_work_in_russian(self):
        roster = {'p:scout': '', 'p:reviewer': '', 'p:builder': '', 'q:codex-rescue': '', 'q:scriptwriter': '', 'solo': '', 'Explore': ''}
        office = server.auto_office('/work/app', roster)
        zones = {z['name']: [d['name'] for d in z['desks']] for z in office['zones']}
        self.assertEqual(zones, {'РАЗВЕДКА': ['Главный', 'Скаут'], 'ПЛАН': ['Сценарист'], 'ПРОИЗВОДСТВО': ['Сборщик'],
                                 'КАЧЕСТВО': ['Ревьюер'], 'ПОДДЕРЖКА': ['Спасатель'], 'ПРОЧИЕ': ['Solo']})
        self.assertEqual([z['name'] for z in office['zones']], ['РАЗВЕДКА', 'ПЛАН', 'ПРОИЗВОДСТВО', 'КАЧЕСТВО', 'ПОДДЕРЖКА', 'ПРОЧИЕ'])
        self.assertTrue(all(d['prop'] in server.PROPS for z in office['zones'] for d in z['desks']))
        self.assertEqual(office['title'], 'ОФИС · APP')

    def test_model_answer_is_tidied(self):
        raw = {
            'title': 'Мастерская',
            'boss': {'name': 'Тимлид', 'prop': 'crown'},
            'zones': [{'name': 'код', 'desks': [
                {'type': 'kit:builder', 'name': 'Сборщик', 'prop': 'box'},
                {'type': 'made:up', 'name': 'Призрак', 'prop': 'cap'},
                {'type': 'kit:builder', 'name': 'Дубль', 'prop': 'box'},
            ]}],
            # the model sat a plugin agent among the guests: it gets a desk, keeping its Russian title
            'guests': [{'type': 'Explore', 'name': 'Разведчик', 'prop': 'binoculars'},
                       {'type': 'kit:reviewer', 'name': 'Проверяльщик', 'prop': 'glasses'}],
        }
        office = server.tidy('/work/app', self.roster, raw)
        zones = {z['name']: [d['name'] for d in z['desks']] for z in office['zones']}
        self.assertEqual(zones, {'КОД': ['Тимлид', 'Сборщик'], 'ПРОЧИЕ': ['Проверяльщик', 'Solo']})
        self.assertEqual(office['guests'], {'Explore': {'name': 'Разведчик', 'prop': 'binoculars'}})
        self.assertEqual(office['title'], 'МАСТЕРСКАЯ')

    def test_latin_titles_from_the_model_are_replaced(self):
        raw = {'title': 'Офис', 'boss': {'name': 'Boss', 'prop': 'crown'},
               'zones': [{'name': 'Build', 'desks': [{'type': 'kit:builder', 'name': 'Builder', 'prop': 'box'}]}], 'guests': []}
        office = server.tidy('/work/app', {'kit:builder': ''}, raw)
        self.assertEqual([d['name'] for d in office['zones'][0]['desks']], ['Главный', 'Сборщик'])
        self.assertEqual(office['zones'][0]['name'], 'ПРОИЗВОДСТВО')


if __name__ == '__main__':
    unittest.main()
