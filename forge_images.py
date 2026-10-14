"""Bounded image transport; media labels come from verified bytes."""
import base64
import io


def provider_image_url(value):
    if not isinstance(value, str) or len(value) > 8_000_000:
        raise ValueError('Image attachment exceeds the limit or has invalid encoding.')
    encoded = value.split(',', 1)[-1] if value.startswith('data:') else value
    try:
        raw = base64.b64decode(encoded, validate=True)
    except (ValueError, TypeError):
        raise ValueError('Invalid image encoding.') from None
    if len(raw) > 6_000_000:
        raise ValueError('Image attachment exceeds the limit.')
    from PIL import Image
    try:
        with Image.open(io.BytesIO(raw)) as picture:
            if picture.width * picture.height > 20_000_000:
                raise ValueError('Image dimensions exceed the limit.')
            media_type = picture.get_format_mimetype()
            picture.verify()
        # Header verification alone accepts truncated JPEG entropy data. Decode
        # within the already checked byte/pixel bounds, preserving original bytes.
        with Image.open(io.BytesIO(raw)) as picture:
            picture.load()
    except (OSError, SyntaxError, Image.DecompressionBombError):
        raise ValueError('Unsupported or invalid image.') from None
    if not isinstance(media_type, str) or not media_type.startswith('image/'):
        raise ValueError('Unsupported image media type.')
    return 'data:' + media_type + ';base64,' + encoded
