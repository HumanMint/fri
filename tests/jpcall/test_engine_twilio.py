import asyncio
import base64
from urllib.parse import parse_qs
from xml.etree.ElementTree import fromstring

import httpx
import pytest
from test_engine_config import environment

from fri.jpcall.config import JpcallConfig
from fri.jpcall.twilio import TwilioClient, TwilioError, validate_signature

SID = 'CA' + '3' * 32


def test_signature_uses_account_auth_token_and_all_sorted_fields():
    # Published Twilio example, independent of our signing implementation.
    assert validate_signature('12345', 'https://example.com/myapp.php?foo=1&bar=2', [
        ('CallSid', 'CA1234567890ABCDE'), ('Caller', '+14158675310'), ('Digits', '1234'),
        ('From', '+14158675310'), ('To', '+18005551212'),
    ], 'L/OH5YylLD5NRKLltdqwSvS0BnU=')
    assert not validate_signature('', 'https://example.test', [], '')
    assert not validate_signature('wrong', 'https://example.test', [], 'é')


def test_rest_api_key_auth_twiml_hangup_and_posted_billing():
    async def run():
        requests = []

        def handle(request):
            requests.append(request)
            assert request.headers['Authorization'] == 'Basic ' + base64.b64encode(
                b'SK22222222222222222222222222222222:synthetic-rest-secret').decode()
            if request.method == 'GET':
                return httpx.Response(200, json={'price': '-0.1234', 'price_unit': 'USD',
                                                 'duration': '61'})
            return httpx.Response(201, json={'sid': SID})

        async with httpx.AsyncClient(transport=httpx.MockTransport(handle)) as http:
            api = TwilioClient(JpcallConfig.from_env(environment()), http)
            assert await api.create_call('03-1234-5678', 'm' * 43, 's' * 43) == SID
            form = parse_qs(requests[0].content.decode())
            assert form['To'] == ['+81312345678']
            assert not any('record' in key.lower() for key in form)
            assert form['TimeLimit'] == ['900']
            assert form['MachineDetection'] == ['Enable'] and form['AsyncAmd'] == ['true']
            assert form['AsyncAmdStatusCallbackMethod'] == ['POST']
            assert form['AsyncAmdStatusCallback'] == form['StatusCallback']
            assert form['StatusCallbackEvent'] == ['initiated', 'ringing', 'answered', 'completed']
            root = fromstring(form['Twiml'][0])
            assert root.find('Connect/Stream').attrib['url'] == (
                'wss://calls.example.test/jpcall/media/' + 'm' * 43)
            assert root.find('Hangup') is not None
            assert not any('record' in node.tag.lower() for node in root.iter())
            assert not any('record' in key.lower() for node in root.iter() for key in node.attrib)
            assert form['StatusCallback'] == [
                'https://calls.example.test/jpcall/status/' + 's' * 43]
            await api.hang_up(SID)
            assert parse_qs(requests[1].content.decode()) == {'Status': ['completed']}
            price = await api.call_price(SID)
            assert price.duration_s == 61 and price.usd == .1234
            await api.close()
            assert not http.is_closed
    asyncio.run(run())


@pytest.mark.parametrize('price,unit,expected', [(None, 'USD', None), ('-.25', 'JPY', None),
                                              ('0', 'USD', 0)])
def test_price_pending_currency_and_zero(price, unit, expected):
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(200, json={'price': price, 'price_unit': unit}))) as http:
            result = await TwilioClient(JpcallConfig.from_env(environment()), http).call_price(SID)
            assert result.usd == expected and result.duration_s is None
    asyncio.run(run())


def test_rest_failures_never_include_provider_content_or_routing_tokens():
    async def run():
        async with httpx.AsyncClient(transport=httpx.MockTransport(lambda request:
                httpx.Response(401, text='synthetic-private-provider-body'))) as http:
            with pytest.raises(TwilioError) as error:
                await TwilioClient(JpcallConfig.from_env(environment()), http).create_call(
                    '03-1234-5678', 'm' * 43, 's' * 43)
            assert 'synthetic' not in str(error.value) and 'm' * 43 not in str(error.value)
            assert error.value.__suppress_context__
    asyncio.run(run())
