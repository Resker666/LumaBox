"""Numbered music nodes work while origin, DNS and redirect boundaries remain."""
import io

import pytest

from lumabox.errors import LumaBoxError
from lumabox.extract import extract_gallery_music
from lumabox.gallery_music import _stream_music
from lumabox.urls import validate_audio_url, validate_media_url
from test_gallery_music import mp3_fixture, transport_fixture


WORK = '7695353019219257009'


@pytest.mark.parametrize('host', [
    'sf1-cdn-tos.douyinstatic.com', 'sf16-cdn-tos.douyinstatic.com',
    'sf123-cdn-tos.douyinstatic.com', 'lf1-music-east.douyinstatic.com',
    'lf27-music-east.douyinstatic.com', 'lf123-music-east.douyinstatic.com',
])
def test_new_numbered_player_source_preserves_original_mp3(monkeypatch, mp3_fixture, host):
    source = f'https://{host}/obj/music.mp3?signature=private'
    snapshot = {'work_id': WORK, 'kind': 'gallery', 'scope_confirmed': True,
                'gallery_music': [{'url': source, 'duration': .8}]}
    assert extract_gallery_music(snapshot, WORK) == (source, .8)
    payload = mp3_fixture.read_bytes()
    transport_fixture(monkeypatch, [{'body': payload, 'headers': {'Content-Length': str(len(payload))}}])
    output = io.BytesIO()
    assert _stream_music(source, output, {'Accept-Encoding': 'identity'}, len(payload)) == len(payload)
    assert output.getvalue() == payload
    with pytest.raises(LumaBoxError, match='UNSAFE_TARGET'):
        validate_media_url(source)


@pytest.mark.parametrize('host', ['sf16-cdn-tos.douyinstatic.com', 'lf27-music-east.douyinstatic.com'])
def test_new_numbered_redirect_destination_preserves_bytes(monkeypatch, mp3_fixture, host):
    source = f'https://{host}/obj/music.mp3'
    payload = mp3_fixture.read_bytes()
    transport_fixture(monkeypatch, [{'status':302, 'headers':{'Location':source}},
        {'body':payload, 'headers':{'Content-Length':str(len(payload))}}])
    output = io.BytesIO()
    assert _stream_music('https://lf9-music-east.douyinstatic.com/obj/music.mp3', output,
                         {'Accept-Encoding':'identity'}, len(payload)) == len(payload)
    assert output.getvalue() == payload


@pytest.mark.parametrize('host', [
    'sf16-cdn-tos.douyinstatic.com.evil.test', 'fake.sf16-cdn-tos.douyinstatic.com',
    'sf16-cdn-tos.douyinstatic.org', 'sf16-cdn-tos.douyinstaticcom',
    'sf-cdn-tos.douyinstatic.com', 'sf١٦-cdn-tos.douyinstatic.com',
    'sf16-music-east.douyinstatic.com', 'lf27-cdn-tos.douyinstatic.com',
    'lf27-music-west.douyinstatic.com', 'lf27-music-eastx.douyinstatic.com',
    'douyinstatic.com', 'unrelated.douyinstatic.com',
])
def test_music_family_does_not_accept_other_hosts_or_lookalikes(host):
    for redirect in (False, True):
        with pytest.raises(LumaBoxError) as failure:
            validate_audio_url(f'https://{host}/obj/music.mp3', redirect=redirect)
        assert failure.value.diagnostics['policy_reason'] == 'AUDIO_HOST_UNSUPPORTED'


@pytest.mark.parametrize('source', [
    'http://sf16-cdn-tos.douyinstatic.com/obj/music.mp3',
    'https://user:secret@lf27-music-east.douyinstatic.com/obj/music.mp3',
    'https://sf16-cdn-tos.douyinstatic.com:8443/obj/music.mp3',
    'https://sf16-cdn-tos.douyinstatic.com/obj/music.mp3#fragment',
    'https://lf27-music-east.douyinstatic.com/obj/music%0a.mp3',
])
def test_numbered_family_still_requires_valid_https_url(source):
    with pytest.raises(LumaBoxError) as failure:
        validate_audio_url(source)
    assert failure.value.diagnostics['policy_reason'] == 'AUDIO_URL_INVALID'


@pytest.mark.parametrize('addresses', [
    ('127.0.0.1',), ('10.0.0.1',), ('169.254.169.254',), ('::1',),
    ('198.18.0.1',), ('93.184.216.34', '10.0.0.1'),
])
@pytest.mark.parametrize('redirect', [False, True])
def test_numbered_source_and_redirect_keep_real_public_dns_checks(monkeypatch, addresses, redirect):
    import lumabox.gallery_music as music
    import lumabox.net as net
    destination = 'sf16-cdn-tos.douyinstatic.com'
    url = f'https://{destination}/obj/music.mp3'
    responses = [{'status':302, 'headers':{'Location':url}}] if redirect else []
    connections = transport_fixture(monkeypatch, responses)
    # Keep the production resolver and IP classifier; replace only OS DNS.
    monkeypatch.setattr(music, 'resolve_public', net.resolve_public)

    def lookup(host, port, **kwargs):
        values = addresses if host == destination else ('93.184.216.34',)
        return [(net.socket.AF_INET6 if ':' in address else net.socket.AF_INET,
                 net.socket.SOCK_STREAM, 6, '', (address, port)) for address in values]

    monkeypatch.setattr(net.socket, 'getaddrinfo', lookup)
    initial = 'https://lf9-music-east.douyinstatic.com/obj/music.mp3' if redirect else url
    output = io.BytesIO()
    with pytest.raises(LumaBoxError) as failure:
        _stream_music(initial, output, {'Accept-Encoding':'identity'}, 100)
    assert failure.value.code == 'UNSAFE_TARGET'
    assert failure.value.diagnostics.get('network_reason') == 'DNS_NON_PUBLIC'
    assert failure.value.diagnostics['host'] == destination
    assert output.getvalue() == b''
    assert connections == ([('lf9-music-east.douyinstatic.com','93.184.216.34')] if redirect else [])
