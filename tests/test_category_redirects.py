"""Incoming new redirects: source has no category, direct target is watched."""
import tempfile
import unittest
from dataclasses import replace
from datetime import datetime, timezone
from pathlib import Path
from unittest.mock import AsyncMock

from change_monitor import WikiChangeMonitor
from mediawiki_client import MediaWikiClient, CategorySnapshot
from test_change_monitor import FakeClient, sample_change


def alias_change(**kwargs):
    return replace(sample_change(), change_type='new', title='新别名', pageid=99,
                   old_revid=0, **kwargs)


class TargetLookupTests(unittest.IsolatedAsyncioTestCase):
    async def test_normalization_and_variant_conversion_without_following_redirects(self):
        client=MediaWikiClient('https://example.org/api.php')
        client._request=AsyncMock(return_value={'query':{
            'normalized':[{'from':'page_name','to':'Page name'}],
            'converted':[{'from':'Page name','to':'规范名'}],
            'pages':[{'title':'规范名','pageid':42}, {'title':'Missing','missing':True},
                     {'title':'Alias','pageid':77,'redirect':True}],
            'interwiki':[{'title':'en:Page','url':'https://example.net/Page'}]}})
        result=await client.page_ids_for_titles(['page_name','Missing','Alias','en:Page'])
        self.assertEqual(result,{'page_name':42,'Missing':0,'Alias':77,'en:Page':0})
        params=client._request.call_args.args[0]
        self.assertNotIn('redirects',params)
        self.assertEqual(params['prop'],'info')
        self.assertEqual(params['converttitles'],'1')

    async def test_target_lookup_batches_and_deduplicates(self):
        client=MediaWikiClient('https://example.org/api.php')
        client._request=AsyncMock(return_value={'query':{'pages':[]}})
        self.assertEqual(await client.page_ids_for_titles([]),{})
        client._request.assert_not_awaited()
        await client.page_ids_for_titles([str(i) for i in range(51)]+['0'])
        self.assertEqual(client._request.await_count,2)
        self.assertEqual(len(client._request.call_args_list[0].args[0]['titles'].split('\x1f'))-1,50)


class IncomingRedirectTests(unittest.IsolatedAsyncioTestCase):
    def client(self, target='目标#章节', **changes):
        client=FakeClient()
        change=alias_change(**changes)
        client.changes=[change]
        client.revision_redirects=AsyncMock(return_value={change.revid:target})
        client.page_ids_for_titles=AsyncMock(return_value={'目标':42})
        return client

    async def test_uncategorized_alias_is_sent_and_remains_outside_membership(self):
        client=self.client()
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
            sub=await m.subscribe('group:1','SIFAC')
            await m.poll_once()
            self.assertEqual(sender.await_count,1)
            message=sender.call_args.args[1]
            self.assertIn('新别名',message)
            self.assertIn('新建重定向 → ｢目标#章节｣',message)
            self.assertIn('按重定向目标匹配订阅分类',message)
            self.assertIn('curid=99',message)
            self.assertNotIn('diff=',message)
            self.assertNotIn(99,sub.member_page_ids)
            client.page_ids_for_titles.assert_awaited_once_with(['目标'])
            client.revision_redirects.assert_awaited_once_with([client.changes[0].revid])

    async def test_source_and_target_matching_merge_one_notification_per_group(self):
        client=self.client()
        client.page_category_map={99:{'Category:SIFAC'}}
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
            await m.subscribe('group:1','SIFAC')
            await m.subscribe('group:1','Other')
            await m.subscribe('group:2','SIFAC')
            await m.poll_once()
            self.assertEqual(sender.await_count,2)
            self.assertEqual({call.args[0] for call in sender.call_args_list},{'group:1','group:2'})
            group1=next(call.args[1] for call in sender.call_args_list if call.args[0]=='group:1')
            self.assertIn('SIFAC',group1)
            self.assertIn('Other',group1)

    async def test_no_subscribed_category_or_disabled_switch_does_not_scan_new_aliases(self):
        for mode in ('page-only','disabled','detection-disabled'):
            with self.subTest(mode=mode),tempfile.TemporaryDirectory() as d:
                client=self.client()
                sender=AsyncMock()
                m=WikiChangeMonitor(client,Path(d)/'state.json',sender,
                                    watch_category_redirects=mode!='disabled',
                                    detect_redirects=mode!='detection-disabled')
                if mode=='page-only':
                    await m.subscribe_page('group:1','目标')
                else:
                    await m.subscribe('group:1','SIFAC')
                await m.poll_once()
                sender.assert_not_awaited()
                client.revision_redirects.assert_not_awaited()
                client.page_ids_for_titles.assert_not_awaited()

    async def test_missing_external_nonmember_and_plain_new_pages_do_not_match(self):
        for target,ids in [('目标',{'目标':0}),('en:目标',{'en:目标':0}),
                           ('其他条目',{'其他条目':777}),('',{}),('#本页章节',{})]:
            with self.subTest(target=target),tempfile.TemporaryDirectory() as d:
                client=self.client(target)
                client.page_ids_for_titles=AsyncMock(return_value=ids)
                sender=AsyncMock()
                m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
                await m.subscribe('group:1','SIFAC')
                await m.poll_once()
                sender.assert_not_awaited()

    async def test_editing_an_unwatched_existing_alias_is_not_a_new_redirect_event(self):
        client=self.client()
        client.changes=[replace(client.changes[0],change_type='edit',old_revid=1)]
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
            await m.subscribe('group:1','SIFAC')
            await m.poll_once()
            sender.assert_not_awaited()
            client.page_ids_for_titles.assert_not_awaited()

    async def test_notification_survives_restart_without_resending(self):
        client=self.client()
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            path=Path(d)/'state.json'
            m=WikiChangeMonitor(client,path,sender)
            await m.subscribe('group:1','SIFAC')
            await m.poll_once()
            m=WikiChangeMonitor(client,path,sender)
            await m.poll_once()
            self.assertEqual(sender.await_count,1)

    async def test_failed_target_lookup_retries_recent_alias_without_consuming_cursor(self):
        now=datetime.now(timezone.utc).isoformat(timespec='seconds').replace('+00:00','Z')
        client=self.client(timestamp=now)
        client.page_ids_for_titles.side_effect=RuntimeError('offline')
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
            await m.subscribe('group:1','SIFAC')
            with self.assertLogs('change_monitor',level='WARNING'):
                await m.poll_once()
            self.assertNotIn(client.changes[0].rcid,m.state.recent_rcids)
            sender.assert_not_awaited()
            client.page_ids_for_titles.side_effect=None
            await m.poll_once()
            self.assertEqual(sender.await_count,1)

    async def test_hidden_revision_is_not_assumed_to_be_redirect(self):
        client=self.client()
        client.revision_redirects.return_value={}
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
            await m.subscribe('group:1','SIFAC')
            await m.poll_once()
            sender.assert_not_awaited()
            client.page_ids_for_titles.assert_not_awaited()

    async def test_target_in_previous_snapshot_or_subcategory_is_matched(self):
        client=self.client()
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
            await m.subscribe('group:1','Root')
            client.category_tree=AsyncMock(return_value=CategorySnapshot(page_ids=set(),titles=set(),categories=['Category:Root']))
            m._category_refresh_at.clear()
            await m.poll_once()
            self.assertEqual(sender.await_count,1)

    async def test_before_subscription_start_is_not_scanned(self):
        client=self.client(timestamp='2020-01-01T00:00:00Z')
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
            await m.subscribe('group:1','SIFAC')
            await m.poll_once()
            sender.assert_not_awaited()
            client.revision_redirects.assert_not_awaited()

    async def test_move_related_uncategorized_alias_still_deduplicates(self):
        client=self.client(target='目标')
        creation=client.changes[0]
        move=replace(sample_change(),title='新别名',change_type='move',logid=7,
                     move_target='目标',suppress_redirect=False,rcid=creation.rcid+1)
        client.changes=[creation,move]
        sender=AsyncMock()
        with tempfile.TemporaryDirectory() as d:
            m=WikiChangeMonitor(client,Path(d)/'state.json',sender)
            await m.subscribe('group:1','SIFAC')
            await m.poll_once()
            self.assertEqual(sender.await_count,1)
            self.assertIn('移动至',sender.call_args.args[1])
