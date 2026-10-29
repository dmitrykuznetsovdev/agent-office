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
        server.TAILS.clear()

    def tearDown(self):
        server.PROJECTS = self.projects
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

    def test_auto_office_groups_by_plugin_and_skips_builtins(self):
        office = server.auto_office('/work/app', self.roster)
        zones = {z['name']: [d['type'] for d in z['desks']] for z in office['zones']}
        self.assertEqual(zones, {'KIT': ['main', 'kit:reviewer', 'kit:builder'], 'СВОИ АГЕНТЫ': ['solo']})
        self.assertTrue(all(d['prop'] in server.PROPS for z in office['zones'] for d in z['desks']))

    def test_model_answer_is_tidied(self):
        raw = {
            'title': 'Мастерская',
            'boss': {'name': 'Тимлид', 'prop': 'crown'},
            'zones': [{'name': 'код', 'desks': [
                {'type': 'kit:builder', 'name': 'Сборщик', 'prop': 'box'},
                {'type': 'made:up', 'name': 'Призрак', 'prop': 'cap'},
                {'type': 'kit:builder', 'name': 'Дубль', 'prop': 'box'},
            ]}],
            'guests': [{'type': 'Explore', 'name': 'Разведчик', 'prop': 'binoculars'}],
        }
        office = server.tidy('/work/app', self.roster, raw)
        zones = {z['name']: [d['name'] for d in z['desks']] for z in office['zones']}
        self.assertEqual(zones, {'КОД': ['Тимлид', 'Сборщик'], 'ПРОЧИЕ': ['Reviewer', 'Solo']})
        self.assertEqual(office['guests'], {'Explore': {'name': 'Разведчик', 'prop': 'binoculars'}})
        self.assertEqual(office['title'], 'МАСТЕРСКАЯ')


if __name__ == '__main__':
    unittest.main()
