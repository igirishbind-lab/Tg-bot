"""
Pure Python fallback for imghdr (removed in Python 3.13).
Detects image type based on file headers.
"""

def what(file, h=None):
    if h is None:
        try:
            with open(file, 'rb') as f:
                h = f.read(32)
        except Exception:
            return None

    # JPEG
    if h.startswith(b'\xff\xd8'):
        return 'jpeg'

    # PNG
    if h.startswith(b'\211PNG\r\n\032\n'):
        return 'png'

    # GIF
    if h[:6] in (b'GIF87a', b'GIF89a'):
        return 'gif'

    # WEBP
    if h.startswith(b'RIFF') and h[8:12] == b'WEBP':
        return 'webp'

    # BMP
    if h.startswith(b'BM'):
        return 'bmp'

    # ICO
    if h.startswith(b'\x00\x00\x01\x00'):
        return 'ico'

    # TIFF
    if h.startswith(b'II*\x00') or h.startswith(b'MM\x00*'):
        return 'tiff'

    return None
