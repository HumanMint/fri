"""Public webhook ingress through the real listen() runner, fed in memory without sockets."""

import asyncio
import base64
import gc
import hashlib
import hmac
import tracemalloc
import zlib
from urllib.parse import urlencode

import pytest
from test_engine_config import environment

import fri.jpcall.server as server_module
from fri.jpcall.config import JpcallConfig

SID = 'CA' + '4' * 32


class Transport(asyncio.Transport):
    def __init__(self):
        super().__init__()
        self.out = bytearray()
        self.closed = False

    def get_extra_info(self, name, default=None):
        return {'peername': ('203.0.113.9', 4444), 'sockname': ('127.0.0.1', 8790)}.get(name, default)

    def write(self, data):
        self.out.extend(data)

    def is_closing(self):
        return self.closed

    def close(self):
        self.closed = True

    def pause_reading(self):
        pass

    def resume_reading(self):
        pass

    def get_write_buffer_size(self):
        return 0

    def set_write_buffer_limits(self, high=None, low=None):
        pass


async def listening(monkeypatch):
    class Site:  # The runner, handler factory and app are the production ones; only TCP is skipped.
        def __init__(self, runner, host, port):
            pass

        async def start(self):
            pass

    monkeypatch.setattr(server_module.web, 'TCPSite', Site)
    server = server_module.JpcallServer(JpcallConfig.from_env(environment()))
    return server, await server.listen()


async def post(runner, path, body, *headers):
    transport = Transport()
    protocol = runner.server()
    raw = b'\r\n'.join([b'POST ' + path.encode() + b' HTTP/1.1', b'Host: calls.example.test',
                        b'Content-Length: ' + str(len(body)).encode(), b'Connection: close',
                        *headers]) + b'\r\n\r\n' + body
    gc.collect()
    tracemalloc.start()
    try:
        protocol.connection_made(transport)
        protocol.data_received(raw)  # One read, as an asyncio socket delivers up to 256 KiB.
        for _ in range(500):
            if transport.out:
                break
            await asyncio.sleep(0)
        peak = tracemalloc.get_traced_memory()[1]
    finally:
        tracemalloc.stop()
        protocol.connection_lost(None)
    return int(bytes(transport.out).split(b' ', 2)[1]), peak


FORM = b'Content-Type: application/x-www-form-urlencoded'


@pytest.mark.parametrize('encoding', [b'deflate', b'gzip', b'identity'])
def test_encoded_status_body_is_refused_without_inflating(monkeypatch, encoding):
    async def run():
        server, runner = await listening(monkeypatch)
        try:
            body = zlib.compress(b'a' * (64 * 1024 * 1024), 9)  # About 64 KB on the wire.
            status, peak = await post(runner, '/jpcall/status/unknown', body, FORM,
                                      b'Content-Encoding: ' + encoding)
            assert status == 415
            assert peak < 512 * 1024  # The runner never inflates (inflating peaks over 1 MiB).
        finally:
            await runner.cleanup()
    asyncio.run(run())


@pytest.mark.parametrize('body,content_type', [
    (b'\xda\x1f\x8b' * 100, FORM),
    (b'CallSid=' + SID.encode(), FORM + b'; charset=not-a-charset'),
])
def test_undecodable_status_form_is_a_clean_client_error(monkeypatch, body, content_type):
    async def run():
        server, runner = await listening(monkeypatch)
        try:
            assert (await post(runner, '/jpcall/status/unknown', body, content_type))[0] == 400
        finally:
            await runner.cleanup()
    asyncio.run(run())


def test_signed_plain_callback_and_size_cap_still_work(monkeypatch):
    async def run():
        server, runner = await listening(monkeypatch)
        seen = []

        async def on_status(params):
            seen.append(params)

        try:
            routes = server.register(None, None, on_status)
            path = '/jpcall/status/' + routes.status_token
            params = {'AccountSid': server.config.twilio_account_sid, 'CallSid': SID,
                      'CallStatus': 'ringing', 'SequenceNumber': '0'}
            payload = server.config.public_base_url + path + ''.join(
                key + params[key] for key in sorted(params))
            signature = base64.b64encode(hmac.new(server.config.twilio_auth_token.encode(),
                                                  payload.encode(), hashlib.sha1).digest())
            status, _ = await post(runner, path, urlencode(params).encode(), FORM,
                                   b'X-Twilio-Signature: ' + signature)
            assert status == 204
            assert seen == [{key: params[key] for key in params if key != 'AccountSid'}]
            assert (await post(runner, path, b'a=' + b'b' * (17 * 1024), FORM))[0] == 413
        finally:
            await runner.cleanup()
    asyncio.run(run())
