import io
import os
import re
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
FONT_DIRS = (
    "C:/Windows/Fonts",
    "/usr/share/fonts/truetype",
    "/usr/share/fonts",
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
    if not TEMPLATE_PATH.is_file():
        raise ValueError("FaithFLY.psd is missing from the backend. Keep it beside app.py and redeploy.")
    data = TEMPLATE_PATH.read_bytes()

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


def _normalize_font_name(value):
    if not value or not isinstance(value, str):
        return ""
    value = value.strip()
    if not value:
        return ""
    value = value.replace("_", " ")
    value = re.sub(r"\s+", " ", value)
    return value


def _font_candidates(font_name):
    normalized = _normalize_font_name(font_name)
    names = []
    if normalized:
        names.extend([
            normalized,
            normalized.lower(),
            normalized.replace(" ", ""),
            normalized.replace(" ", "").lower(),
            os.path.splitext(os.path.basename(normalized))[0],
        ])
    for path in FONT_PATHS:
        if path:
            names.append(path)
    return names


def _find_system_font_path(font_name):
    candidates = _font_candidates(font_name)
    seen = set()
    for label in candidates:
        if not label:
            continue
        query = os.path.basename(str(label)).lower()
        if query in seen:
            continue
        seen.add(query)
        for root in FONT_DIRS:
            if not os.path.isdir(root):
                continue
            for file_name in os.listdir(root):
                if not file_name.lower().endswith((".ttf", ".otf", ".ttc")):
                    continue
                full_path = os.path.join(root, file_name)
                file_key = os.path.splitext(file_name)[0].lower()
                if (
                    label and isinstance(label, str) and (
                        file_key == query
                        or file_key == query.replace(" ", "")
                        or file_key == query.replace(" ", "-")
                        or file_key in query.replace(" ", "")
                    )
                ):
                    return full_path
                if os.path.isfile(full_path):
                    base = os.path.splitext(file_name)[0].lower()
                    if base == query or base == query.replace(" ", ""):
                        return full_path
    return None


def _font(size, font_name=None):
    first_choice = _find_system_font_path(font_name) if font_name else None
    if first_choice and Path(first_choice).is_file():
        return ImageFont.truetype(first_choice, size=size)
    for path in FONT_PATHS:
        if path and Path(path).is_file():
            return ImageFont.truetype(path, size=size)
    return ImageFont.load_default(size=size)


def _fit_font(draw, text, max_width, max_height, start_size, font_name=None):
    for size in range(start_size, 17, -2):
        font = _font(size, font_name)
        bounds = draw.textbbox((0, 0), text, font=font)
        if bounds[2] - bounds[0] <= max_width and bounds[3] - bounds[1] <= max_height:
            return font
    return _font(18, font_name)


def _layer_font_name(layer):
    seen = set()

    def walk(value):
        if not value or id(value) in seen:
            return None
        seen.add(id(value))

        if isinstance(value, dict):
            for key in ("font", "fontName", "font_name", "name"):
                if key in value:
                    font_name = value.get(key)
                    if isinstance(font_name, str) and font_name.strip():
                        return font_name.strip()
            for nested in value.values():
                result = walk(nested)
                if result:
                    return result
        elif hasattr(value, "items"):
            return walk(dict(value.items()))
        else:
            for attr in ("font", "fontName", "font_name", "name"):
                if hasattr(value, attr):
                    font_name = getattr(value, attr)
                    if isinstance(font_name, str) and font_name.strip():
                        return font_name.strip()
            for attr in ("text", "engine_dict", "resource_dict"):
                if hasattr(value, attr):
                    result = walk(getattr(value, attr))
                    if result:
                        return result
        return None

    return walk(layer)


def _draw_solid_text(image, text, bounds, start_size, color, font_name=None):
    left, top, right, bottom = bounds
    draw = ImageDraw.Draw(image)
    font = _fit_font(draw, text, right - left, bottom - top, start_size, font_name=font_name)
    draw.text(((left + right) // 2, (top + bottom) // 2), text, fill=color, font=font, anchor="mm")


def _draw_program_text(image, text, bounds, font_name=None):
    left, top, right, bottom = bounds
    draw = ImageDraw.Draw(image)
    font = _fit_font(draw, text, right - left, bottom - top, 125, font_name=font_name)
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


def _apply_text_to_layer(layer, value):
    try:
        if hasattr(layer, "text"):
            layer.text = value
            return True
    except Exception:
        pass
    return False


def _render_flyer(photo, title, program, place):
    psd, layers = _load_template()

    text_layers = {
        "Title here": title,
        "Program name": program,
        "Place here": place,
    }
    for layer_name, value in text_layers.items():
        layer = layers[layer_name]
        if not _apply_text_to_layer(layer, value):
            raise ValueError(f"The PSD layer '{layer_name}' is not editable as text. Keep it as a text layer in the design and try again.")

    canvas = psd.composite().convert("RGBA")

    photo_layer = layers["Photo here"]
    left, top, right, bottom = photo_layer.bbox
    if right <= left or bottom <= top:
        raise ValueError("The template photo layer has invalid dimensions.")

    fitted_photo = ImageOps.fit(photo, (right - left, bottom - top), method=Image.Resampling.LANCZOS)
    fitted_photo = fitted_photo.convert("RGBA")
    canvas.paste(fitted_photo, (left, top), fitted_photo)

    output = io.BytesIO()
    canvas.convert("RGB").save(output, format="JPEG", quality=92, optimize=True)
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
