"""Deterministic code-native Forge icon; no remote assets or font dependency."""
from pathlib import Path
from PIL import Image, ImageDraw

root=Path(__file__).resolve().parents[1]
canvas=Image.new('RGBA',(1024,1024),(0,0,0,0)); draw=ImageDraw.Draw(canvas)
draw.rounded_rectangle((0,0,1023,1023),radius=224,fill='#191a1c')
draw.polygon([(x*4,y*4) for x,y in ((64,66),(195,66),(175,96),(99,96),(99,125),(168,125),(148,155),(99,155),(99,208),(64,208))],fill='#e98544')
draw.polygon([(x*4,y*4) for x,y in ((174,145),(196,112),(220,148),(198,181))],fill='#f2b37e')
image=canvas.resize((256,256),Image.Resampling.LANCZOS)
image.save(root/'assets/forge.png')
image.save(root/'assets/forge.ico',sizes=[(16,16),(24,24),(32,32),(48,48),(64,64),(128,128),(256,256)])
