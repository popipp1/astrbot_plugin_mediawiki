"""Move/redirect API, durable delivery, and compact preview regressions."""
import tempfile
import unittest
from dataclasses import replace
from pathlib import Path
from unittest.mock import AsyncMock

from mediawiki_client import MediaWikiClient, parse_redirect_target
from change_monitor import (WikiChangeMonitor, format_change_notification, parse_diff_html,
                            redirect_preview, DiffLine, _source_visible)
from test_change_monitor import sample_change, FakeClient
from test_structured_preview import added, paired, context


class EventApiTests(unittest.IsolatedAsyncioTestCase):
    async def test_move_without_revision_and_unrelated_logs(self):
        client = MediaWikiClient('https://example.org/api.php')
        rows = [dict(rcid=1, type='log', title='Old', pageid=42, revid=0, logid=7,
                     logtype='move', logaction='move', logparams={'target_title':'New','suppressredirect':True}),
                dict(rcid=2, type='log', logtype='delete', revid=0),
                dict(rcid=3, type='log', logtype='move', logaction='move', logparams={})]
        client._request = AsyncMock(return_value={'query':{'recentchanges':rows}})
        changes = await client.recent_changes('2026-09-07T00:00:00Z')
        self.assertEqual(len(changes),1)
        self.assertEqual(changes[0].change_type,'move')
        self.assertEqual(changes[0].move_target,'New')
        self.assertTrue(changes[0].suppress_redirect)
        self.assertEqual(changes[0].logid,7)
        self.assertIn('loginfo',client._request.call_args.args[0]['rcprop'])

    async def test_move_disabled_and_incomplete_hidden_log(self):
        client = MediaWikiClient('https://example.org/api.php')
        client._request = AsyncMock(return_value={'query':{'recentchanges':[]}})
        await client.recent_changes('2026-09-07T00:00:00Z',include_moves=False)
        self.assertEqual(client._request.call_args.args[0]['rctype'],'edit|new')

    async def test_redirects_use_exact_content_not_latest_page_flags(self):
        client = MediaWikiClient('https://example.org/api.php')
        client._request = AsyncMock(side_effect=[
            {'query':{'magicwords':[{'name':'redirect','aliases':['#REDIRECT','#重定向']}]}},
            {'query':{'pages':[{'redirect':True,'revisions':[
                {'revid':1,'slots':{'main':{'contentmodel':'wikitext','content':'普通正文'}}},
                {'revid':2,'slots':{'main':{'contentmodel':'wikitext','content':'#重定向 [[目标#章节]]'}}},
                {'revid':3,'slots':{'main':{'contentmodel':'wikitext','texthidden':True}}},
                {'revid':4,'slots':{'main':{'contentmodel':'javascript','content':'#REDIRECT [[X]]'}}},
                {'revid':5,'slots':{'main':{'contentmodel':'wikitext','content':'#REDIRECT [['}}}
            ]}]}}])
        self.assertEqual(await client.revision_redirects([1,2,3,4,5]),{1:'',2:'目标#章节'})
        params = client._request.call_args.args[0]
        self.assertEqual(params['revids'],'1|2|3|4|5')
        self.assertEqual(params['rvslots'],'main')
        self.assertNotIn('titles',params)

    async def test_redirect_alias_cache_and_batch_limit(self):
        client = MediaWikiClient('https://example.org/api.php')
        client._redirect_aliases = ['#REDIRECT']
        client._request = AsyncMock(return_value={'query':{'pages':[]}})
        self.assertEqual(await client.revision_redirects([]),{})
        client._request.assert_not_awaited()
        await client.revision_redirects(list(range(1,43)))
        self.assertEqual(client._request.await_count,3)
        self.assertEqual(len(client._request.call_args_list[0].args[0]['revids'].split('|')),20)


class EventFormatTests(unittest.TestCase):
    def test_redirect_parser_boundaries(self):
        aliases=['#REDIRECT','#重定向']
        for source,target in [(' #redirect [[A_B#C]]','A B#C'), ('#重定向：[[A]]',None),
                              ('#重定向 [[A]]','A'), ('正文\n#REDIRECT [[A]]',''),
                              ('<!--x-->#REDIRECT [[A]]',None), ('{{T}}',''),
                              ('#REDIRECT [[{{T}}]]',None), ('#REDIRECT [[]]',None)]:
            with self.subTest(source=source):
                # Unknown punctuation must not be promoted to an event.
                actual=parse_redirect_target(source,aliases)
                if source == '#重定向：[[A]]':
                    self.assertFalse(actual)
                else:
                    self.assertEqual(actual,target)

    def test_all_redirect_transitions_and_unknown(self):
        c=sample_change()
        for old,new,label in [('', 'A','改为重定向'), ('A','B','重定向目标'), ('A','','取消重定向')]:
            self.assertIn(label,redirect_preview(c,{c.old_revid:old,c.revid:new})[0].text)
        self.assertEqual(redirect_preview(c,{c.revid:'B'}),[])
        self.assertEqual(redirect_preview(c,{c.old_revid:'A',c.revid:'A'}),[])
        self.assertEqual(redirect_preview(c,{c.old_revid:'',c.revid:''}),[])

    def test_new_redirect_uses_article_link(self):
        c=replace(sample_change(),change_type='new',old_revid=0)
        message=format_change_notification(c,[],redirect_preview(c,{c.revid:'目标#章节'}),api_url='https://example.org/api.php')
        self.assertIn('新建重定向 → ｢目标#章节｣',message)
        self.assertIn('curid=42',message)
        self.assertNotIn('diff=',message)

    def test_move_has_log_link_and_no_fake_byte_delta(self):
        c=replace(sample_change(),change_type='move',logid=7,move_target='New',suppress_redirect=True,revid=0)
        message=format_change_notification(c,[],[],api_url='https://example.org/api.php')
        self.assertIn('logid=7',message)
        self.assertIn('📦移动至｢New｣',message)
        self.assertIn('未保留旧标题重定向',message)
        self.assertNotIn('+95',message)
        self.assertNotIn('diff=',message)


class EventWorkflowTests(unittest.IsolatedAsyncioTestCase):
    async def test_move_delivered_without_compare_and_survives_restart(self):
        client=FakeClient()
        client.changes=[replace(sample_change(),change_type='move',revid=0,old_revid=0,logid=7,move_target='New')]
        client.compare_revisions=AsyncMock()
        client.revision_content_models=AsyncMock()
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json'
            m=WikiChangeMonitor(client,p,sender)
            sub=await m.subscribe_page('group:1',sample_change().title)
            await m.poll_once()
            self.assertEqual(sub.target,'New')
            self.assertEqual(sender.await_count,1)
            client.compare_revisions.assert_not_awaited()
            client.revision_content_models.assert_not_awaited()
            m=WikiChangeMonitor(client,p,sender)
            await m.poll_once()
            self.assertEqual(sender.await_count,1)

    async def test_move_created_redirect_dedup_is_same_operation_and_durable(self):
        client=FakeClient()
        move=replace(sample_change(),change_type='move',logid=7,move_target='New',suppress_redirect=False)
        redirect=replace(sample_change(),rcid=102,change_type='new',old_revid=0,pageid=99,revid=99)
        client.changes=[redirect,move]  # reversed API ordering
        client.revision_redirects=AsyncMock(return_value={redirect.revid:'New'})
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json'
            m=WikiChangeMonitor(client,p,sender)
            await m.subscribe('group:1','SIFAC')
            await m.poll_once()
            self.assertEqual(sender.await_count,1)
            self.assertIn('移动至',sender.call_args.args[1])
            self.assertEqual(set(m.state.recent_rcids),{101,102})
            # The related new-page row stays suppressed after restart.
            client.changes=[replace(redirect,rcid=103)]
            m=WikiChangeMonitor(client,p,sender)
            await m.poll_once()
            self.assertEqual(sender.await_count,1)
            # A genuinely later manual redirect revision must still be sent.
            client.changes=[replace(redirect,rcid=104,revid=99,timestamp='2026-09-04T01:58:00Z')]
            client.revision_redirects=AsyncMock(return_value={99:'New'})
            await m.poll_once()
            self.assertEqual(sender.await_count,2)

    async def test_move_failure_is_queued_and_retryable(self):
        client=FakeClient()
        client.changes=[replace(sample_change(),change_type='move',move_target='New',logid=7)]
        sender=AsyncMock(side_effect=RuntimeError('offline'))
        with tempfile.TemporaryDirectory() as d:
            p=Path(d)/'state.json'
            m=WikiChangeMonitor(client,p,sender)
            await m.subscribe_page('group:1',sample_change().title)
            with self.assertLogs('change_monitor',level='ERROR'):
                await m.poll_once()
            self.assertEqual(len(m.state.pending),1)
            sender.side_effect=None
            m=WikiChangeMonitor(client,p,sender)
            await m.poll_once()
            self.assertEqual(len(m.state.pending),0)

    async def test_redirect_failure_retains_ordinary_edit(self):
        client=FakeClient()
        client.revision_redirects=AsyncMock(side_effect=RuntimeError('unavailable'))
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
            await m.subscribe_page('group:1',sample_change().title)
            with self.assertLogs('change_monitor',level='WARNING'):
                await m.poll_once()
            self.assertEqual(sender.await_count,1)
            self.assertIn('diff=',sender.call_args.args[1])
            self.assertNotIn('取消重定向',sender.call_args.args[1])

    async def test_redirect_dedup_does_not_leak_between_recipients_or_editors(self):
        client=FakeClient()
        c=replace(sample_change(),change_type='new',pageid=99,old_revid=0)
        client.changes=[c]
        client.revision_redirects=AsyncMock(return_value={c.revid:'New'})
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
            await m.subscribe('group:1','SIFAC')
            await m.subscribe('group:2','SIFAC')
            m.state.move_redirects=[dict(pageid=42,title=c.title,target='New',timestamp=c.timestamp,
                                         user=c.user,umos=['group:1'])]
            await m.poll_once()
            self.assertEqual(sender.await_count,1)
            self.assertEqual(sender.call_args.args[0],'group:2')
            client.changes=[replace(c,rcid=202,user='Another editor')]
            await m.poll_once()
            self.assertEqual(sender.await_count,3)

    async def test_switches_disable_move_and_redirect_reads(self):
        client=FakeClient()
        client.revision_redirects=AsyncMock()
        client.changes=[replace(sample_change(),change_type='move',move_target='New')]
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender,include_moves=False,detect_redirects=False)
            await m.subscribe_page('group:1',sample_change().title)
            await m.poll_once()
            sender.assert_not_awaited()
            client.changes=[sample_change()]
            await m.poll_once()
            client.revision_redirects.assert_not_awaited()

    async def test_different_redirect_target_is_not_suppressed(self):
        client=FakeClient()
        move=replace(sample_change(),change_type='move',logid=7,move_target='New',suppress_redirect=False)
        client.changes=[move,replace(sample_change(),rcid=102,change_type='new',pageid=99,revid=99,old_revid=0)]
        client.revision_redirects=AsyncMock(return_value={99:'Different target'})
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
            await m.subscribe('group:1','SIFAC')
            await m.poll_once()
            self.assertEqual(sender.await_count,2)

    async def test_redirect_cancel_keeps_other_body_diff(self):
        client=FakeClient()
        c=sample_change()
        client.revision_redirects=AsyncMock(return_value={c.old_revid:'A',c.revid:''})
        client.compare_revisions=AsyncMock(return_value=added('新的正文'))
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
            await m.subscribe_page('group:1',c.title)
            await m.poll_once()
            self.assertIn('取消重定向',sender.call_args.args[1])
            self.assertIn('新的正文',sender.call_args.args[1])

    async def test_redirect_enabled_even_when_text_preview_disabled(self):
        client=FakeClient()
        c=sample_change()
        client.revision_redirects=AsyncMock(return_value={c.old_revid:'A',c.revid:'B'})
        client.compare_revisions=AsyncMock()
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender,max_diff_lines=0)
            await m.subscribe_page('group:1',c.title)
            await m.poll_once()
            self.assertIn('重定向目标',sender.call_args.args[1])
            client.compare_revisions.assert_not_awaited()

    async def test_new_old_title_does_not_hijack_moved_page_subscription(self):
        client=FakeClient()
        client.changes=[replace(sample_change(),change_type='new',pageid=99)]
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
            await m.subscribe_page('group:1',sample_change().title)
            await m.poll_once()
            sender.assert_not_awaited()


class CompactPriorityTests(unittest.TestCase):
    def test_new_heading_year_and_body_are_one_event(self):
        html=added('')+added('== 電視動畫 ==')+added("'''2026年'''")+added('* 絲皮卡——[[DARK MACHINE]]')
        lines=parse_diff_html(html,max_lines=1)
        self.assertEqual(lines[0].action,'section_add')
        self.assertEqual(lines[0].replacement,'電視動畫 / 2026年')
        self.assertIn('絲皮卡',lines[0].text)
        self.assertIn('空行调整',lines[-1].text)

    def test_whitespace_does_not_take_content_slot(self):
        lines=parse_diff_html(added('')+added('actual'),max_lines=1)
        self.assertEqual(lines[0].text,'actual')

    def test_no_heading_group_across_context_or_opposite_side(self):
        for barrier in [context('unchanged'),'<tr><td class="diff-deletedline">other</td></tr>']:
            lines=parse_diff_html(added('== Heading ==')+barrier+added('body'))
            self.assertNotIn('section_add',[x.action for x in lines])

    def test_pure_insertion_deletion_is_short_but_mixed_stays_safe(self):
        for old,new,action in [('前后','前<ins>附带的</ins>后','local_add'),
                               ('右<del>手手</del>腕','右腕','local_delete')]:
            lines=parse_diff_html(paired(old,new))
            self.assertEqual(lines[0].action,action)
            self.assertTrue(lines[0].context)
        lines=parse_diff_html(paired('ABC','X<ins>Y</ins>Z'))
        self.assertEqual(lines[0].action,'before_after')

    def test_short_link_has_word_context(self):
        lines=parse_diff_html(paired('透明','[[蝴蝶兰|透]]明'))
        self.assertEqual(lines[0].context,'透明')

    def test_source_clip_prefers_complete_boundary(self):
        text='正常内容'*8+'[[一个很长很长的链接目标]]'
        result=_source_visible(text,40)
        self.assertNotIn('[[',result)
        self.assertLessEqual(len(result),40)
        result=_source_visible('{{T|'+'x'*100+'}}',40)
        self.assertIn('源码截断',result)
        self.assertLessEqual(len(result),40)

    def test_position_not_repeated_on_adjacent_replacements(self):
        lines=[DiffLine('replace','1','2',show_position=True,positions=((66,66),)),
               DiffLine('replace','3','4',show_position=True,positions=((66,66),))]
        message=format_change_notification(sample_change(),[],lines,api_url='https://example.org/api.php')
        self.assertEqual(message.count('第66行'),1)
        self.assertIn('同一行',message)

    def test_long_event_titles_obey_budget(self):
        for change in [replace(sample_change(),change_type='move',move_target='目标'*2000,logid=7),
                       replace(sample_change(),change_type='new')]:
            lines=redirect_preview(change,{change.revid:'目标'*2000})
            message=format_change_notification(change,[],lines,api_url='https://example.org/api.php',max_message_chars=500)
            self.assertLessEqual(len(message),500)
            self.assertNotIn('diff=',message)
