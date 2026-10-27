"""Synthetic visual checks; no member photos, Discord access or stored dialogue."""

import base64
import struct
import zlib


def image():
    """A red circle on the left and a blue square on the right, without text labels."""
    def paint(x, y):
        if (x-48)**2+(y-48)**2 <= 26**2:
            return (255, 0, 0)
        if 118 <= x <= 170 and 22 <= y <= 74:
            return (0, 0, 255)
        return (255, 255, 255)
    return png(192, 96, paint)


def shape(name, color):
    """One unlabelled shape, filled with color, on white: stand-ins for memes posted in a channel."""
    inside = {'circle': lambda x, y: (x-48)**2+(y-48)**2 <= 30**2,
              'square': lambda x, y: 20 <= x <= 76 and 20 <= y <= 76,
              'triangle': lambda x, y: 16 <= y <= 80 and abs(x-48) <= (y-16)/2}[name]
    return png(96, 96, lambda x, y: color if inside(x, y) else (255, 255, 255))


def png(width, height, paint):
    rows = [b'\0'+bytes(value for x in range(width) for value in paint(x, y)) for y in range(height)]
    def chunk(kind, body):
        return struct.pack('!I', len(body))+kind+body+struct.pack('!I', zlib.crc32(kind+body))
    data = (b'\x89PNG\r\n\x1a\n'+chunk(b'IHDR', struct.pack('!2I5B', width, height, 8, 2, 0, 0, 0))+
            chunk(b'IDAT', zlib.compress(b''.join(rows)))+chunk(b'IEND', b''))
    return 'data:image/png;base64,'+base64.b64encode(data).decode()


def cases():
    return [
        {'name': 'informal_photo_left', 'text': 'color and shape on the left', 'images': [image()],
         'visual_words': ('red', 'circle'), 'expect': 'Identify the red circle from pixels, without claiming the photo is missing.'},
        {'name': 'informal_photo_right', 'text': 'color and shape on the right', 'images': [image()],
         'visual_words': ('blue', 'square'), 'expect': 'Identify the blue square from pixels, without claiming the photo is missing.'},
    ]
