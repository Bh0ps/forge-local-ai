"""Generate the app and browser icons from the code-native Forge monogram.

The SVG is the source of truth. No external renderer, fonts or assets are needed.
Only the small SVG vocabulary used by this mark is accepted.
"""
from pathlib import Path
import re
import shutil
import xml.etree.ElementTree as ET
from PIL import Image, ImageDraw

root = Path(__file__).resolve().parents[1]
scale = 4
canvas = Image.new('RGBA', (256 * scale, 256 * scale), (0, 0, 0, 0))
draw = ImageDraw.Draw(canvas)


def path_points(source):
    tokens = re.findall(r'[MLCZ]|-?\d+(?:\.\d+)?', source)
    points = []; index = 0; current = (0, 0)
    while index < len(tokens):
        command = tokens[index]; index += 1
        if command == 'Z':
            points.append(points[0]); continue
        count = 6 if command == 'C' else 2
        numbers = [float(value) for value in tokens[index:index + count]]; index += count
        if command in ('M', 'L'):
            current = tuple(numbers); points.append(current)
        elif command == 'C':
            p0 = current; p1 = numbers[:2]; p2 = numbers[2:4]; p3 = numbers[4:]
            for step in range(1, 33):
                t = step / 32; u = 1 - t
                points.append(tuple(u**3*p0[axis] + 3*u*u*t*p1[axis] + 3*u*t*t*p2[axis] + t**3*p3[axis] for axis in (0, 1)))
            current = tuple(p3)
        else:
            raise ValueError('Unsupported logo path command: ' + command)
    return [(round(x * scale), round(y * scale)) for x, y in points]


for element in ET.parse(root / 'assets/forge.svg').getroot():
    tag = element.tag.rsplit('}', 1)[-1]; attr = element.attrib
    if tag == 'rect':
        draw.rounded_rectangle((0, 0, 256*scale-1, 256*scale-1), radius=float(attr['rx'])*scale, fill=attr['fill'])
    elif tag == 'ellipse':
        cx, cy, rx, ry = (float(attr[key]) for key in ('cx', 'cy', 'rx', 'ry'))
        draw.ellipse(((cx-rx)*scale, (cy-ry)*scale, (cx+rx)*scale, (cy+ry)*scale), fill=attr['fill'])
    elif tag == 'path':
        points = path_points(attr['d'])
        if attr.get('fill') != 'none':
            draw.polygon(points, fill=attr['fill'])
        else:
            width = round(float(attr['stroke-width'])*scale)
            draw.line(points, fill=attr['stroke'], width=width, joint='curve')
            for x, y in (points[0], points[-1]):
                radius = width/2; draw.ellipse((x-radius,y-radius,x+radius,y+radius), fill=attr['stroke'])
    else:
        raise ValueError('Unsupported logo element: ' + tag)
image = canvas.resize((256, 256), Image.Resampling.LANCZOS)
image.save(root / 'assets/forge.png')
image.save(root / 'assets/forge.ico', sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
shutil.copyfile(root / 'assets/forge.svg', root / 'frontend/public/forge.svg')
for size in (16, 32, 48, 128):
    icon = canvas.resize((size, size), Image.Resampling.LANCZOS)
    icon.save(root / f'browser-extension/icons/forge-{size}.png')
