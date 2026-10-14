import base64
from copy import deepcopy
import io
import threading

from PIL import Image
import pytest

from forge_images import provider_image_url
from forge_inference import CompatibleProvider


def pixels(format):
    buffer = io.BytesIO()
    Image.new('RGB', (8, 4), '#b06a42').save(buffer, format=format)
    return buffer.getvalue()


@pytest.mark.parametrize('format,media', [('PNG', 'image/png'), ('JPEG', 'image/jpeg'),
    ('WEBP', 'image/webp'), ('GIF', 'image/gif'), ('BMP', 'image/bmp'), ('TIFF', 'image/tiff')])
def test_provider_labels_real_pixels_without_reencoding(format, media):
    raw = pixels(format)
    encoded = base64.b64encode(raw).decode('ascii')
    url = provider_image_url(encoded)
    assert url == 'data:' + media + ';base64,' + encoded
    assert base64.b64decode(url.split(',', 1)[1]) == raw


def test_misleading_declared_media_type_cannot_override_actual_jpeg():
    encoded = base64.b64encode(pixels('JPEG')).decode('ascii')
    assert provider_image_url('data:image/png;base64,' + encoded) == 'data:image/jpeg;base64,' + encoded


@pytest.mark.parametrize('missing', [1, 2], ids=['missing-marker-byte', 'missing-marker'])
def test_truncated_jpeg_is_refused_before_provider_dispatch(missing):
    encoded = base64.b64encode(pixels('JPEG')[:-missing]).decode('ascii')
    with pytest.raises(ValueError, match='Unsupported or invalid image'):
        provider_image_url(encoded)


@pytest.mark.parametrize('value', [None, 12, {}, 'forge-attachment:synthetic', 'not base64',
    base64.b64encode(b'Synthetic non-image payload').decode('ascii'), 'a' * 8_000_001],
    ids=['null', 'number', 'object', 'unresolved-reference', 'encoding', 'non-image', 'oversize'])
def test_invalid_image_is_refused_with_content_free_error(value):
    with pytest.raises(ValueError) as error:
        provider_image_url(value)
    assert 'Synthetic' not in str(error.value)


@pytest.mark.parametrize('format,media', [('PNG', 'image/png'), ('JPEG', 'image/jpeg'), ('WEBP', 'image/webp')])
def test_actual_compatible_payload_preserves_image_and_input(format, media, monkeypatch):
    class Captured(Exception): pass
    class InspectProvider(CompatibleProvider):
        def prepare_body(self, data, body):
            self.observed = body
            raise Captured()
    def forbidden(*args, **kwargs):
        pytest.fail('Pixel transport fixture must not use network or credentials')
    monkeypatch.setattr('forge_inference.httpx.AsyncClient', forbidden)
    provider = InspectProvider({'url': 'http://synthetic.invalid/v1'})
    monkeypatch.setattr(provider, '_headers', forbidden)
    encoded = base64.b64encode(pixels(format)).decode('ascii')
    data = {'model': 'synthetic-vision', 'context': 8192,
            'messages': [{'role': 'user', 'content': 'Synthetic pixel fixture', 'images': [encoded]}]}
    before = deepcopy(data)
    with pytest.raises(Captured): list(provider.generate(data, threading.Event()))
    message = next(message for message in provider.observed['messages'] if message['role'] == 'user')
    assert message['content'][1]['image_url']['url'] == 'data:' + media + ';base64,' + encoded
    assert 'images' not in message and data == before
