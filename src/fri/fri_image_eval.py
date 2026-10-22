"""Synthetic visual checks; no member photos, Discord access or stored dialogue."""

import base64
import struct
import zlib


def image():
    """A red circle on the left and a blue square on the right, without text labels."""
    rows = []
    for y in range(96):
        pixels = bytearray()
        for x in range(192):
            color = (255, 255, 255)
            if (x-48)**2+(y-48)**2 <= 26**2:
                color = (255, 0, 0)
            elif 118 <= x <= 170 and 22 <= y <= 74:
                color = (0, 0, 255)
            pixels.extend(color)
        rows.append(b'\0'+pixels)
    def chunk(kind, body):
        return struct.pack('!I', len(body))+kind+body+struct.pack('!I', zlib.crc32(kind+body))
    png = (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('!2I5B', 192, 96, 8, 2, 0, 0, 0))+
           chunk(b'IDAT', zlib.compress(b''.join(rows)))+chunk(b'IEND', b''))
    return 'data:image/png;base64,'+base64.b64encode(png).decode()


def cases():
    return [
        {'name': 'informal_photo_left', 'text': 'color and shape on the left', 'images': [image()],
         'visual_words': ('red', 'circle'), 'expect': 'Identify the red circle from pixels, without claiming the photo is missing.'},
        {'name': 'informal_photo_right', 'text': 'color and shape on the right', 'images': [image()],
         'visual_words': ('blue', 'square'), 'expect': 'Identify the blue square from pixels, without claiming the photo is missing.'},
    ]
