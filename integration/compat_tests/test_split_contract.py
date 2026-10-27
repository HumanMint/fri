"""Serialize through the real controller; never share in-process schema instances."""

import asyncio
import json
from types import SimpleNamespace
from unittest.mock import Mock

import pytest
from gene import fri_agent_store, reviews

from fri.fri import Fri
from fri.fri_agent_settings import DEFAULTS, RANGES
from fri.fri_errors import ControllerRejected
from fri.fri_protocol import CAPABILITIES, PROTOCOL, compatible, contract
from fri.reviews import REVIEW_ID, request_reference


class Session:
    def __init__(self, client, override=None):
        self.client, self.override = client, override
        self.calls=[]

    def request(self, method, url, *, json=None, headers=None):
        self.calls.append((method,url,headers))
        response=self.client.request(method,url.replace('http://controller',''),json=json,headers=headers)
        value=self.override if self.override is not None else response.json()
        class Result:
            status=response.status_code
            async def json(self):
                return value
            async def __aenter__(self):
                return self
            async def __aexit__(self,*_):
                pass
        return Result()


def test_runtime_client_handshake_and_wrong_service_actions(client):
    async def scenario():
        runtime=SimpleNamespace(url='http://controller',token='fri-'+'x'*32,session=Session(client))
        assert await Fri.api(runtime,'GET','/fri/contract')==contract()
        assert runtime.session.calls[0][2]['X-Fri-Contract']==PROTOCOL
        for path in ('/worker/claim','/deploy/claim'):
            with pytest.raises(ControllerRejected):
                await Fri.api(runtime,'POST',path)
    asyncio.run(scenario())


@pytest.mark.parametrize('response',[{}, {**contract(),'protocol':'gene-fri/2'},
                                    {**contract(),'capabilities':[]}])
def test_incompatible_controller_cannot_start_background_or_discord_views(client,monkeypatch,response):
    async def scenario():
        import fri.fri as module
        runtime=SimpleNamespace(session=None,api=None,add_view=Mock())
        runtime.api=lambda method,path: Fri.api(SimpleNamespace(url='http://controller',
            token='fri-'+'x'*32,session=runtime.session),method,path)
        monkeypatch.setattr(module.aiohttp,'ClientSession',lambda **kwargs:Session(client,response))
        with pytest.raises(RuntimeError,match='unsupported'):
            await Fri.setup_hook(runtime)
        runtime.add_view.assert_not_called()
        assert len(runtime.session.calls)==1
    asyncio.run(scenario())


def test_settings_hashes_and_persistent_review_components_remain_identical():
    assert DEFAULTS==fri_agent_store.DEFAULTS and RANGES==fri_agent_store.RANGES
    assert REVIEW_ID.pattern==reviews.REVIEW_ID.pattern
    job={'id':'abcdef123456','guild_id':'123','user_id':'10','source_id':'event','source_url':'https://discord.com/channels/1/2/3',
         'body':'fix existing music', 'state':'awaiting_owner','created':123,'kind':'feature'}
    # Wire serialization is the only shared representation.
    assert request_reference(json.loads(json.dumps(job)))==reviews.request_reference(job)


def test_the_author_fence_is_offered_and_optional():
    # Attention uses it when offered (fri #84); a controller without it still runs Fri, on the old reads.
    assert 'author-fence' in contract()['capabilities'] and 'author-fence' not in CAPABILITIES
    assert compatible({**contract(), 'capabilities': [c for c in contract()['capabilities'] if c != 'author-fence']})
