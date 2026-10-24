import asyncio
from unittest.mock import AsyncMock, patch

import pytest

from compat_tests.test_fri_actions import harness
from compat_tests.test_fri_chat import CONTEXT
from compat_tests.test_fri_runtime import event
from fri.fri_actions import Actions
from fri.fri_chat import ChatResult


@pytest.mark.parametrize('gate,expected_calls', [('investigate', 2), ('quiet', 1), ('failed', 1)])
def test_failure_triage_uses_luna_then_sol_xhigh_without_commands_or_coding(monkeypatch, tmp_path, gate, expected_calls):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        actions = Actions(agent, 20, 1, True)
        actions.event = event(user=20, channel=1, content='gene didnt reply')
        actions.event.webhook_id = None
        actions.event.channel.fetch_message = AsyncMock(return_value=actions.event)
        actions.message = actions.event.content
        actions.scope = {'guild_id': '123', 'user_id': '20', 'channel_id': '1', 'conversation_id': 'public:1'}
        receipts = [{'channel_id': '1', 'result': {'uncertain': 'Gene restarted'}},
                    {'channel_id': '2', 'result': {'output': 'Private other channel'}}]
        fri.api = AsyncMock(side_effect=[{'revision': 0}, {'responsive': False}, {'receipts': receipts}])
        agent.run = AsyncMock(side_effect=[
            ({'action': gate, 'source_id': str(actions.event.id)}, {}, 'failed' if gate == 'failed' else 'completed'),
            ({'summary': 'Gene restarted; cause unknown', 'proposed_request': 'Investigate lost command acknowledgements'}, {}, 'completed')])
        result = await actions.execute({'kind': 'gene_diagnose', 'channel_id': ''})
        assert agent.run.await_count == expected_calls
        assert agent.config['attention_model'] == 'gpt-6-luna'
        assert agent.run.await_args_list[0].args[0] == 'attention'
        if gate == 'investigate':
            assert agent.run.await_args.kwargs == {'model': 'gpt-6.1-sol', 'effort': 'xhigh'}
            assert result['proposed_request'] == 'Investigate lost command acknowledgements'
        assert result['receipts'] == receipts[:1]
        assert [call.args[1] for call in fri.api.await_args_list] == ['/chat/context', '/controls/health', '/controls/recent']
        assert all('Private other channel' not in str(call) for call in agent.run.await_args_list)
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('problem', ['lab', 'edited', 'erased', 'no_access'])
def test_diagnosis_revalidates_scope_event_and_erasure(monkeypatch, tmp_path, problem):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        actions = Actions(agent, 20, 1, True)
        actions.event = event(user=20, channel=1, content='gene down')
        actions.event.webhook_id = None
        actions.event.channel.fetch_message = AsyncMock(return_value=actions.event)
        agent.run = AsyncMock()
        fri.api = AsyncMock(return_value={'revision': 1})
        if problem == 'lab':
            actions.guild = 777
        elif problem == 'edited':
            original = event(user=20, channel=1, content='never mind')
            original.webhook_id = None
            actions.event.channel.fetch_message.return_value = original
        elif problem == 'no_access':
            agent.member.return_value = None
        result = await actions.execute({'kind': 'gene_diagnose', 'channel_id': ''})
        assert 'blocked' in result
        agent.run.assert_not_awaited()
        assert all(c.args[1] != '/controls/health' for c in fri.api.await_args_list)
        await fri.close()
    asyncio.run(scenario())


def test_diagnosis_is_available_with_empty_catalog_and_offers_real_review_without_submission(monkeypatch, tmp_path):
    async def scenario():
        fri, agent, _, _ = harness(monkeypatch, tmp_path)
        source = event(user=20, channel=1, content='gene doesnt reply')
        actions = Actions(agent, 20, 1, True)
        actions.event = source
        fri.api = AsyncMock(return_value={'actions': []})
        offered = await actions.available()
        assert 'gene_diagnose' in offered and 'gene_command' not in offered
        calls = []
        async def api(method, path, body=None):
            calls.append((path, body))
            if path == '/agent/admit':
                return {'allowed': True}
            if path == '/chat/context':
                return {**CONTEXT, 'revision': 0}
            return {}
        fri.api = api
        fri.chat.respond = AsyncMock(side_effect=[
            ChatResult(status='completed', action={'kind': 'gene_diagnose', 'channel_id': ''}),
            ChatResult(status='completed', reply='gene restarted, cause is still unclear. got a repair preview for you')])
        receipt = {'kind': 'gene_diagnose', 'proposed_request': 'Investigate the missed command', 'diagnosis_status': 'completed'}
        with patch.object(Actions, 'execute', new=AsyncMock(return_value=receipt)):
            result = await fri.talk(123, 1, 20, source.content, 'public', event=source)
        await fri.record_turn(result)  # As after a successful Discord delivery.
        assert result.draft == receipt['proposed_request']
        assert all(path not in ('/requests', '/controls') for path, _ in calls)
        saved = next(body for path, body in calls if path == '/chat/turn')
        assert saved['source']['user_id'] == '20' and 'gene_diagnose' in saved['reply']
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('outcome',['accepted','owner_review','unknown','rejected'])
def test_verified_member_report_submits_only_to_bounded_repair_intake(monkeypatch,tmp_path,outcome):
    from fri import feedback as commands
    async def scenario():
        fri,agent,_,_=harness(monkeypatch,tmp_path)
        agent.state['auto_repairs_enabled']=True
        actions=Actions(agent,20,1,True)
        actions.event=event(user=20,channel=1,content='gene vote count is broken')
        actions.event.webhook_id=None
        actions.event.channel.fetch_message=AsyncMock(return_value=actions.event)
        actions.message=actions.event.content
        actions.scope={'guild_id':'123','user_id':'20','channel_id':'1','conversation_id':'public:1'}
        actions.valid=AsyncMock(return_value=True)
        called=[]
        async def api(method,path,body=None):
            called.append((path,body))
            if path=='/chat/context':
                return {'revision':0}
            if path=='/controls/health':
                return {'responsive':True}
            if path=='/controls/recent':
                return {'receipts':[]}
            if path=='/repairs/recent':
                return {'repairs':[]}
            if path=='/repairs':
                if outcome=='rejected':
                    from fri.fri_errors import ControllerRejected
                    raise ControllerRejected('Repair checks need a fresh Discord message')
                if outcome=='unknown':
                    raise commands.UserFeedbackCheckFailure('No acknowledgement')
                return {'accepted':outcome=='accepted','id':'a'*12,'state':'queued'}
            raise AssertionError(path)
        fri.api=api
        agent.run=AsyncMock(side_effect=[
            ({'action':'investigate','source_id':str(actions.event.id)},{},'completed'),
            ({'summary':'This needs code-level verification.','proposed_request':'Check the existing vote count.'},{},'completed')])
        result=await actions.execute({'kind':'gene_diagnose','channel_id':''})
        submitted=[body for path,body in called if path=='/repairs']
        assert len(submitted)==1 and submitted[0]['source']['user_id']=='20'
        assert submitted[0]['revision']==0
        assert all(path not in ('/requests','/controls') for path,_ in called)
        if outcome in ('accepted','unknown'):
            assert result['proposed_request']==''
        else:
            assert result['proposed_request']=='Check the existing vote count.'
        if outcome=='unknown':
            assert result['repair']['uncertain']
        await fri.close()
    asyncio.run(scenario())


def test_auto_repair_receipt_does_not_attach_a_duplicate_request_preview(monkeypatch,tmp_path):
    async def scenario():
        fri,_,_,_=harness(monkeypatch,tmp_path)
        source=event(user=20,channel=1,content='gene count broke')
        async def api(method,path,body=None):
            if path=='/agent/admit':
                return {'allowed':True}
            if path=='/chat/context':
                return {**CONTEXT,'revision':0}
            return {}
        fri.api=api
        fri.chat.respond=AsyncMock(side_effect=[
            ChatResult(status='completed',action={'kind':'gene_diagnose','channel_id':''}),
            ChatResult(status='completed',reply='got it, the worker is checking it',draft='incorrect duplicate draft')])
        receipt={'kind':'gene_diagnose','proposed_request':'','repair':{'accepted':True,'state':'queued','id':'a'*12}}
        with patch.object(Actions,'execute',new=AsyncMock(return_value=receipt)):
            result=await fri.talk(123,1,20,source.content,'public',event=source)
        assert result.draft is None
        await fri.close()
    asyncio.run(scenario())


@pytest.mark.parametrize('status',[400,403,422,500])
def test_controller_rejection_is_distinct_from_an_uncertain_server_outcome(monkeypatch,tmp_path,status):
    from unittest.mock import Mock

    from fri import feedback as commands
    from fri.fri_errors import ControllerRejected
    async def scenario():
        fri,_,_,_=harness(monkeypatch,tmp_path)
        response=Mock(status=status)
        response.json=AsyncMock(return_value={'detail':'Repair checks need a fresh Discord message'})
        context=AsyncMock()
        context.__aenter__.return_value=response
        session=Mock()
        session.request.return_value=context
        original=fri.session
        fri.session=session
        try:
            with pytest.raises(commands.UserFeedbackCheckFailure) as error:
                await fri.api('POST','/repairs',{})
            assert isinstance(error.value,ControllerRejected)==(status<500)
        finally:
            fri.session=original
            await fri.close()
    asyncio.run(scenario())
