import base64
import io
import random

from mcp.server.fastmcp import FastMCP
from PIL import Image, ImageFilter

mcp = FastMCP("img-proc")


def _decode(b64: str) -> Image.Image:
    return Image.open(io.BytesIO(base64.b64decode(b64)))


def _encode(img: Image.Image) -> str:
    buf = io.BytesIO()
    img.save(buf, format="PNG")
    return base64.b64encode(buf.getvalue()).decode()


@mcp.tool()
def blur(image_b64: str, radius: float = 2.0) -> str:
    """Apply Gaussian blur to an image. Returns base64-encoded PNG."""
    img = _decode(image_b64).filter(ImageFilter.GaussianBlur(radius))
    return _encode(img)


@mcp.tool()
def rotate(image_b64: str, angle: float = 90.0) -> str:
    """Rotate image by angle degrees counter-clockwise. expand=True preserves full image. Returns base64-encoded PNG."""
    img = _decode(image_b64).rotate(angle, expand=True)
    return _encode(img)


@mcp.tool()
def flip(image_b64: str, direction: str = "horizontal") -> str:
    """Flip image horizontally or vertically. direction must be 'horizontal' or 'vertical'. Returns base64-encoded PNG."""
    img = _decode(image_b64)
    if direction == "horizontal":
        img = img.transpose(Image.FLIP_LEFT_RIGHT)
    elif direction == "vertical":
        img = img.transpose(Image.FLIP_TOP_BOTTOM)
    else:
        raise ValueError("direction must be 'horizontal' or 'vertical'")
    return _encode(img)


@mcp.tool()
def resize(image_b64: str, width: int = 256, height: int = 256) -> str:
    """Resize image to given width x height pixels. Returns base64-encoded PNG."""
    if width <= 0 or height <= 0:
        raise ValueError("width and height must be positive integers")
    img = _decode(image_b64).resize((width, height))
    return _encode(img)


@mcp.tool()
def crop(image_b64: str, x1: int = 0, y1: int = 0, x2: int = 100, y2: int = 100) -> str:
    """Crop image to bounding box [x1, y1, x2, y2] where (0,0) is top-left. Returns base64-encoded PNG."""
    img = _decode(image_b64)
    w, h = img.size
    if x1 < 0 or y1 < 0 or x2 > w or y2 > h or x1 >= x2 or y1 >= y2:
        raise ValueError(f"Invalid crop ({x1},{y1},{x2},{y2}) for image size {w}x{h}")
    return _encode(img.crop((x1, y1, x2, y2)))


@mcp.tool()
def add_noise(image_b64: str, amount: float = 0.05) -> str:
    """Add salt-and-pepper noise. amount is fraction of pixels to corrupt (0.0-1.0). Returns base64-encoded PNG."""
    if not 0.0 <= amount <= 1.0:
        raise ValueError("amount must be between 0.0 and 1.0")
    img = _decode(image_b64).convert("RGB")
    pixels = img.load()
    w, h = img.size
    for _ in range(int(w * h * amount)):
        x = random.randint(0, w - 1)
        y = random.randint(0, h - 1)
        pixels[x, y] = (255, 255, 255) if random.random() > 0.5 else (0, 0, 0)
    return _encode(img)


if __name__ == "__main__":
    import uvicorn
    uvicorn.run(mcp.sse_app(), host="0.0.0.0", port=8000)
