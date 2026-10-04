import io
import os
from pathlib import Path

from flask import Flask, jsonify, request, send_file
from PIL import Image, ImageDraw, ImageFont, ImageOps
from psd_tools import PSDImage
from werkzeug.exceptions import RequestEntityTooLarge
from werkzeug.utils import secure_filename

app = Flask(__name__)
app.config["MAX_CONTENT_LENGTH"] = 32 * 1024 * 1024

TEMPLATE_PATH = Path(__file__).with_name("FaithFLY.psd")
REPLACEABLE_LAYERS = {"Title here", "Program name", "Place here", "Photo here"}
FONT_PATHS = (
    os.environ.get("FAITHFLY_FONT"),
    "/usr/share/fonts/truetype/dejavu/DejaVuSerif.ttf",
    "/usr/share/fonts/truetype/liberation2/LiberationSerif-Regular.ttf",
    "C:/Windows/Fonts/georgia.ttf",
    "C:/Windows/Fonts/times.ttf",
)
Image.MAX_IMAGE_PIXELS = 25_000_000


def _cors_origins():
    return {
        origin.strip()
        for origin in os.environ.get("FRONTEND_ORIGINS", "*").split(",")
        if origin.strip()
    }


@app.after_request
def add_cors_headers(response):
    origins = _cors_origins()
    origin = request.headers.get("Origin", "")
    if "*" in origins:
        response.headers["Access-Control-Allow-Origin"] = "*"
    elif origin in origins:
        response.headers["Access-Control-Allow-Origin"] = origin
    response.headers["Access-Control-Allow-Methods"] = "GET, POST, OPTIONS"
    response.headers["Access-Control-Allow-Headers"] = "Content-Type"
    response.headers["Access-Control-Max-Age"] = "600"
    response.headers["Vary"] = "Origin"
    return response


@app.errorhandler(RequestEntityTooLarge)
def request_too_large(_error):
    return jsonify(error="The uploaded files exceed the 32 MB request limit."), 413


def _text_field(name):
    value = " ".join(request.form.get(name, "").split())
    if not value:
        raise ValueError(f"The {name} field is required.")
    if len(value) > 200:
        raise ValueError(f"The {name} field must be 200 characters or fewer.")
    return value


def _load_photo():
    upload = request.files.get("photo")
    if upload is None or not upload.filename:
        raise ValueError("A PNG photo is required.")

    try:
        image = Image.open(upload.stream)
        if image.format != "PNG":
            raise ValueError("The photo must be a PNG file.")
        if image.width * image.height > Image.MAX_IMAGE_PIXELS:
            raise ValueError("The photo dimensions are too large.")
        image.load()
        return image.convert("RGBA")
    except (OSError, Image.DecompressionBombError) as error:
        raise ValueError("The uploaded photo is not a valid PNG image.") from error


def _load_template():
    upload = request.files.get("template")
    if upload is not None and upload.filename:
        data = upload.read()
    elif TEMPLATE_PATH.is_file():
        data = TEMPLATE_PATH.read_bytes()
    else:
        raise ValueError("Upload the FaithFLY PSD template.")

    if len(data) < 26 or data[:4] != b"8BPS":
        raise ValueError("The template must be a valid Photoshop PSD file.")

    try:
        psd = PSDImage.open(io.BytesIO(data))
    except Exception as error:
        raise ValueError("The template PSD could not be opened.") from error

    if psd.width > 6000 or psd.height > 6000:
        raise ValueError("The template dimensions must be 6000 pixels or smaller.")

    layers = {}
    for layer in psd.descendants():
        if layer.name in REPLACEABLE_LAYERS:
            if layer.name in layers:
                raise ValueError(f"The template has more than one '{layer.name}' layer.")
            layers[layer.name] = layer

    missing = REPLACEABLE_LAYERS - layers.keys()
    if missing:
        raise ValueError("The template is missing layers: " + ", ".join(sorted(missing)))

    for layer in layers.values():
        layer.visible = False

    return psd, layers


def _font(size):
    for path in FONT_PATHS:
        if path and Path(path).is_file():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default(size=size)


def _fit_font(draw, text, max_width, max_height, start_size):
    for size in range(start_size, 17, -2):
        font = _font(size)
        bounds = draw.textbbox((0, 0), text, font=font)
        if bounds[2] - bounds[0] <= max_width and bounds[3] - bounds[1] <= max_height:
            return font
    return _font(18)


def _draw_solid_text(image, text, bounds, start_size, color):
    left, top, right, bottom = bounds
    draw = ImageDraw.Draw(image)
    font = _fit_font(draw, text, right - left, bottom - top, start_size)
    draw.text(((left + right) // 2, (top + bottom) // 2), text, fill=color, font=font, anchor="mm")


def _draw_program_text(image, text, bounds):
    left, top, right, bottom = bounds
    draw = ImageDraw.Draw(image)
    font = _fit_font(draw, text, right - left, bottom - top, 125)
    center = ((left + right) // 2, (top + bottom) // 2)
    mask = Image.new("L", image.size, 0)
    ImageDraw.Draw(mask).text(center, text, fill=255, font=font, anchor="mm")

    top_color = (75, 111, 235)
    bottom_color = (110, 226, 255)
    height = image.height
    span = max(bottom - top, 1)
    gradient = Image.new("RGB", (1, height))
    gradient.putdata([
        tuple(
            round(top_color[channel] + (bottom_color[channel] - top_color[channel]) * min(max((y - top) / span, 0), 1))
            for channel in range(3)
        )
        for y in range(height)
    ])
    gradient = gradient.resize(image.size, Image.Resampling.BILINEAR)
    image.paste(Image.composite(gradient, image, mask))


def _render_flyer(photo, title, program, place):
    psd, layers = _load_template()
    canvas = psd.composite().convert("RGB")

    photo_layer = layers["Photo here"]
    left, top, right, bottom = photo_layer.bbox
    if right <= left or bottom <= top:
        raise ValueError("The template photo layer has invalid dimensions.")
    fitted_photo = ImageOps.fit(photo, (right - left, bottom - top), method=Image.Resampling.LANCZOS)
    canvas.paste(fitted_photo.convert("RGB"), (left, top))

    _draw_solid_text(canvas, title, layers["Title here"].bbox, 38, "white")
    _draw_program_text(canvas, program, layers["Program name"].bbox)
    _draw_solid_text(canvas, place, layers["Place here"].bbox, 58, "white")

    output = io.BytesIO()
    canvas.save(output, format="JPEG", quality=92, optimize=True)
    output.seek(0)
    return output


@app.get("/api/health")
def health():
    return jsonify(status="ok")


@app.post("/api/generate")
def generate():
    try:
        title = _text_field("title")
        program = _text_field("program")
        place = _text_field("place")
        photo = _load_photo()
        output = _render_flyer(photo, title, program, place)
        filename = secure_filename(program) or "FaithFLY"
        return send_file(
            output,
            mimetype="image/jpeg",
            as_attachment=False,
            download_name=f"{filename}.jpg",
            max_age=0,
        )
    except ValueError as error:
        return jsonify(error=str(error)), 400
    except Exception:
        app.logger.exception("Flyer rendering failed")
        return jsonify(error="The server could not process this PSD. Check the template and backend logs."), 500


if __name__ == "__main__":
    app.run(host="0.0.0.0", port=int(os.environ.get("PORT", "5000")))
