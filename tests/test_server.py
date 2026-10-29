import json
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.join(os.path.dirname(__file__), '..'))
import server  # noqa: E402


def line(**entry):
    return json.dumps(entry, ensure_ascii=False) + '\n'


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


class StateTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.TemporaryDirectory()
        self.projects, server.PROJECTS = server.PROJECTS, self.dir.name
        server.TAILS.clear()

    def tearDown(self):
        server.PROJECTS = self.projects
        server.TAILS.clear()
        self.dir.cleanup()

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
