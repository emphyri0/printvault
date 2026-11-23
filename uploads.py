"""Files that belong to prints and printers: names, types, storing and removing."""
import os
import re
import uuid

import db
import thumbs
from db import UPLOAD_DIR
from thumbs import PHOTO_EXT

THUMBS = thumbs.Worker(UPLOAD_DIR)   # WebP thumbnails, made in the background
MODEL_EXT = {"3mf", "stl", "step", "stp", "obj", "gcode"}


def ext_of(filename):
    return filename.rsplit(".", 1)[-1].lower() if "." in filename else ""


def clean_name(filename, ext):
    """Safe file name for display and download. Letters of every script stay; folders, control characters
    and characters that Windows refuses do not. Always ends in .<ext>."""
    name = "".join(c for c in re.split(r"[\\/]", filename)[-1] if c.isprintable())
    stem = re.sub(rf"\.?{re.escape(ext)}$", "", re.sub(r'[<>:"|?*]', "_", name), flags=re.I).strip(" .")
    return f"{(stem or 'file')[:150 - len(ext) - 1]}.{ext}"


def download_name(f):
    """original_name, with the extension that older versions lost for names like '日本.3mf'."""
    name, ext = f["original_name"], ext_of(f["stored_name"])
    return name if ext_of(name) or not ext else f"{name}.{ext}"


def photo_type(upload):
    """Extension that matches the content of an uploaded photo, or None when it is not a photo."""
    ext = thumbs.sniff(upload.stream.read(thumbs.SNIFF_BYTES))
    upload.stream.seek(0)
    return ext


def new_name(ext):
    return f"{uuid.uuid4().hex}.{ext}"


def add_row(con, print_id, kind, stored, original_name, size):
    """Registers a stored file with a print and returns its id."""
    return db.insert(con, "files", {"print_id": print_id, "kind": kind, "stored_name": stored,
                                    "original_name": original_name, "size": size})


def add_photo(con, print_id, content, original_name):
    """Stores photo bytes with a print; does nothing when the content is not a photo."""
    ext = thumbs.sniff(content)
    if ext is None:
        return
    stored = new_name(ext)
    with open(os.path.join(UPLOAD_DIR, stored), "wb") as fh:
        fh.write(content)
    add_row(con, print_id, "photo", stored, original_name, len(content))
    THUMBS.add(stored)


def save(con, print_id, uploads):
    """Stores uploaded files with a print and commits. Returns the names of the files that were skipped."""
    skipped = []
    for f in uploads:
        if not f or not f.filename:
            continue
        ext = ext_of(f.filename)
        original_name = clean_name(f.filename, ext)
        if ext in PHOTO_EXT:
            kind, ext = "photo", photo_type(f)
        else:
            kind = "model" if ext in MODEL_EXT else None
        if kind is None or ext is None:
            skipped.append(f.filename)
            continue
        stored = new_name(ext)
        path = os.path.join(UPLOAD_DIR, stored)
        f.save(path)
        add_row(con, print_id, kind, stored, original_name, os.path.getsize(path))
        if kind == "photo":
            THUMBS.add(stored)
    con.commit()
    return skipped


def save_printer_photo(upload):
    """Stores an uploaded printer photo and returns its stored_name, or None when it is not a photo."""
    ext = photo_type(upload) if ext_of(upload.filename) in PHOTO_EXT else None
    if ext is None:
        return None
    stored = new_name(ext)
    upload.save(os.path.join(UPLOAD_DIR, stored))
    THUMBS.add(stored)
    return stored


def remove(stored_name):
    if not stored_name:
        return
    try:
        os.remove(os.path.join(UPLOAD_DIR, stored_name))
    except FileNotFoundError:
        pass
    thumbs.remove(UPLOAD_DIR, stored_name)
