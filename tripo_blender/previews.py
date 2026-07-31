"""Thumbnail previews for generated assets.

Tripo returns a `rendered_image` with every finished task. Caching those and
loading them as Blender preview icons is the difference between a history list
you have to decode and one you can actually look at.

Blender's preview system is a fixed pool of ID-keyed icons, so entries are
loaded lazily on first draw and released on unregister.
"""

import os

import bpy
import bpy.utils.previews

_collection = None
_missing = set()      # thumbs we already failed to load; don't retry every redraw


def register():
    global _collection
    if _collection is None:
        _collection = bpy.utils.previews.new()


def unregister():
    global _collection
    if _collection is not None:
        bpy.utils.previews.remove(_collection)
        _collection = None
    _missing.clear()


def icon_id(key, path):
    """Preview icon id for `path`, or 0 if unavailable.

    0 is Blender's "no icon" value, so callers can pass the result straight to
    `layout.template_icon` and get a blank rather than an exception.
    """
    if not path or not os.path.exists(path):
        return 0
    if _collection is None:
        return 0
    if key in _missing:
        return 0

    existing = _collection.get(key)
    if existing is not None:
        return existing.icon_id

    try:
        return _collection.load(key, path, "IMAGE").icon_id
    except Exception as e:
        # A corrupt or half-written thumbnail shouldn't break panel drawing.
        print(f"[tripo] preview load failed for {key}: {e!r}")
        _missing.add(key)
        return 0

