"""The verified sf11 gallery music node works without widening media hosts."""
import io

import pytest

from lumabox.errors import LumaBoxError
from lumabox.extract import extract_gallery_music
from lumabox.gallery_music import _stream_music
from lumabox.urls import validate_audio_url, validate_media_url
from test_gallery_music import mp3_fixture, transport_fixture


SOURCE = 'https://sf11-cdn-tos.douyinstatic.com/obj/ies-music/original.mp3?signature=private'
WORK = '7695353019219257009'


@pytest.mark.parametrize('host', ['sf11-cdn-tos.douyinstatic.com', 'sf6-cdn-tos.douyinstatic.com'])
def test_confirmed_sf_player_streams_its_original_mp3_bytes(monkeypatch, mp3_fixture, host):
    source = SOURCE.replace('sf11-cdn-tos.douyinstatic.com', host)
    payload = mp3_fixture.read_bytes()
    snapshot = {'work_id': WORK, 'kind': 'gallery', 'scope_confirmed': True,
                'gallery_music': [{'url': source, 'duration': 0.8}]}
    assert extract_gallery_music(snapshot, WORK) == (source, 0.8)
    transport_fixture(monkeypatch, [{'body': payload, 'headers': {'Content-Length': str(len(payload))}}])
    output = io.BytesIO()
    assert _stream_music(source, output, {'Accept-Encoding': 'identity'}, len(payload)) == len(payload)
    assert output.getvalue() == payload


@pytest.mark.parametrize('host', ['sf11-cdn-tos.douyinstatic.com', 'sf6-cdn-tos.douyinstatic.com'])
def test_verified_sf_node_can_also_receive_a_music_redirect(monkeypatch, host):
    source = SOURCE.replace('sf11-cdn-tos.douyinstatic.com', host)
    transport_fixture(monkeypatch, [{'status': 302, 'headers': {'Location': source}},
        {'body': b'ID3native', 'headers': {'Content-Length': '9'}}])
    output = io.BytesIO()
    assert _stream_music('https://lf9-music-east.douyinstatic.com/obj/music.mp3', output,
                         {'Accept-Encoding': 'identity'}, 100) == 9
    assert output.getvalue() == b'ID3native'


def test_sf11_music_support_does_not_allow_video_image_or_unverified_neighbors():
    with pytest.raises(LumaBoxError, match='UNSAFE_TARGET'):
        validate_media_url(SOURCE)
    for url in ['https://sf12-cdn-tos.douyinstatic.com/obj/music.mp3',
                'https://sf11-cdn-tos.douyinstatic.com.evil.test/obj/music.mp3',
                'https://douyinstatic.com/obj/music.mp3']:
        with pytest.raises(LumaBoxError) as error:
            validate_audio_url(url)
        assert error.value.code == 'UNSAFE_TARGET'
        assert error.value.diagnostics['policy_reason'] == 'AUDIO_HOST_UNSUPPORTED'
