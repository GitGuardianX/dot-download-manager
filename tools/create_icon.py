"""Rasterize DOT's simple geometric application mark for Windows and the extension."""
from pathlib import Path

from PIL import Image, ImageDraw

root = Path(__file__).resolve().parent.parent
image = Image.new("RGBA", (256, 256), (0, 0, 0, 0))
draw = ImageDraw.Draw(image)
draw.rounded_rectangle((0, 0, 255, 255), radius=56, fill="#151e30")
draw.ellipse((34, 34, 222, 222), fill="#a393ff")
draw.line((128, 73, 128, 164), fill="#0c111d", width=17)
draw.line((85, 129, 128, 174, 171, 129), fill="#0c111d", width=17, joint="curve")
(root / "assets").mkdir(exist_ok=True)
image.save(root / "assets" / "dot.png")
image.save(root / "assets" / "dot.ico", sizes=[(16, 16), (32, 32), (48, 48), (64, 64), (128, 128), (256, 256)])
image.resize((128, 128), Image.Resampling.LANCZOS).save(root / "browser-extension" / "icon.png")
print("Application and extension icons created.")
