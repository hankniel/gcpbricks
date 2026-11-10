"""The console's blog: posts and guides in Markdown, beside the workspace in GCS.

    gs://<bucket>/blog/posts/<slug>.md          a post: YAML front matter, then its Markdown
    gs://<bucket>/blog/assets/<slug>/<file>     the images pasted into it

Everyone in the console reads every post and may write new ones; a post's author and the
console's admins edit and delete it.
"""
from __future__ import annotations

import os
import re
import unicodedata
import uuid
from concurrent.futures import ThreadPoolExecutor
from datetime import UTC, datetime

import requests
import yaml

BLOG = os.environ.get("MLOPS_BLOG", "gs://my-ml-bucket/blog")
_SLUG = re.compile(r"^[a-z0-9]+(-[a-z0-9]+)*$")
#: No SVG: served from the console's own origin, one could run script.
IMAGE_TYPES = {"image/png": "png", "image/jpeg": "jpg", "image/gif": "gif", "image/webp": "webp"}
MAX_IMAGE_BYTES = 10 * 1024 * 1024


def _bucket_prefix() -> tuple[str, str]:
    bucket, _, prefix = BLOG.removeprefix("gs://").partition("/")
    return bucket, prefix.rstrip("/") + "/"


def _post_object(base: str, slug: str) -> str:
    if not _SLUG.match(slug) or len(slug) > 80:
        raise ValueError(f"a post's slug is lowercase letters, digits and dashes, not {slug!r}")
    return f"{base}posts/{slug}.md"


def slugify(title: str) -> str:
    """"Hướng dẫn: Feature Store" -> "huong-dan-feature-store"."""
    ascii_ = unicodedata.normalize("NFKD", title.replace("đ", "d").replace("Đ", "D")).encode("ascii", "ignore").decode()
    return re.sub(r"[^a-z0-9]+", "-", ascii_.lower()).strip("-")[:60].strip("-") or "post"


def parse(text: str) -> tuple[dict, str]:
    """A post's front matter and body."""
    if text.startswith("---\n"):
        head, sep, body = text[4:].partition("\n---\n")
        if sep:
            return yaml.safe_load(head) or {}, body
    return {}, text


def render(meta: dict, body: str) -> str:
    return f"---\n{yaml.safe_dump(meta, allow_unicode=True, sort_keys=False)}---\n{body}"


def _summary(slug: str, meta: dict) -> dict:
    return {"slug": slug, "title": meta.get("title") or slug, "author": meta.get("author", ""),
            "tags": list(meta.get("tags") or []), "summary": meta.get("summary", ""),
            "created": meta.get("created"), "updated": meta.get("updated")}


def _read(gcp, slug: str) -> tuple[dict, str, str]:
    bucket, base = _bucket_prefix()
    got = gcp.read_object(bucket, _post_object(base, slug))
    if got is None:
        raise KeyError(f"no post {slug}")
    meta, body = parse(got[0].decode())
    return meta, body, got[1]


def _can_edit(user: str, meta: dict, admin: bool) -> bool:
    return admin or meta.get("author") == user


def list_posts(gcp) -> list[dict]:
    """Every post, newest change first."""
    bucket, base = _bucket_prefix()
    _, items = gcp.list_dir(bucket, f"{base}posts/")
    slugs = [i["name"][len(base) + len("posts/"):].removesuffix(".md") for i in items if i["name"].endswith(".md")]
    with ThreadPoolExecutor(8) as pool:
        posts = list(pool.map(lambda s: _summary(s, _read(gcp, s)[0]), slugs))
    return sorted(posts, key=lambda p: p["updated"] or "", reverse=True)


def read_post(gcp, user: str, slug: str, admin: bool = False) -> dict:
    meta, body, generation = _read(gcp, slug)
    return {**_summary(slug, meta), "body": body, "generation": generation, "can_edit": _can_edit(user, meta, admin)}


def create_post(gcp, user: str, title: str) -> dict:
    """A new, empty post by `user`, at the first free slug its title gives."""
    title = title.strip()
    if not title:
        raise ValueError("a post needs a title")
    bucket, base = _bucket_prefix()
    now = datetime.now(UTC).isoformat()
    meta = {"title": title, "author": user, "tags": [], "summary": "", "created": now, "updated": now}
    stem = slugify(title)
    for n in range(1, 100):
        slug = stem if n == 1 else f"{stem}-{n}"
        try:
            generation = gcp.write_object(bucket, _post_object(base, slug), render(meta, "").encode(),
                                          "text/markdown; charset=utf-8", "0")
        except requests.HTTPError as e:
            if e.response is not None and e.response.status_code == 412:
                continue
            raise
        gcp.audit({"event": "mlops_console_action", "user": user, "action": "create_post", "slug": slug})
        return {"slug": slug, "generation": generation}
    raise ValueError(f"too many posts are titled {title!r}")


def save_post(gcp, user: str, slug: str, title: str, tags: list[str], summary: str, body: str,
              generation: str | None, admin: bool = False) -> dict:
    """Saves over the generation the editor read, so a save never overwrites a newer one; that
    conflict raises ValueError. The author and the created time stay the post's own."""
    meta, _, current = _read(gcp, slug)
    if not _can_edit(user, meta, admin):
        raise PermissionError(f"{meta.get('author')} and the console's admins edit {slug}, not {user}")
    if not title.strip():
        raise ValueError("a post needs a title")
    meta = {**meta, "title": title.strip(), "tags": [t.strip() for t in tags if t.strip()], "summary": summary.strip(),
            "updated": datetime.now(UTC).isoformat()}
    bucket, base = _bucket_prefix()
    try:
        new = gcp.write_object(bucket, _post_object(base, slug), render(meta, body).encode(),
                               "text/markdown; charset=utf-8", generation or current)
    except requests.HTTPError as e:
        if e.response is not None and e.response.status_code == 412:
            raise ValueError(f"{slug} changed since you opened it: reload it before saving") from None
        raise
    return {"slug": slug, "generation": new}


def delete_post(gcp, user: str, slug: str, admin: bool = False) -> dict:
    meta, _, _ = _read(gcp, slug)
    if not _can_edit(user, meta, admin):
        raise PermissionError(f"{meta.get('author')} and the console's admins delete {slug}, not {user}")
    bucket, base = _bucket_prefix()
    _, assets = gcp.list_dir(bucket, f"{base}assets/{slug}/")
    for a in assets:
        gcp.delete_object(bucket, a["name"])
    gcp.delete_object(bucket, _post_object(base, slug))
    gcp.audit({"event": "mlops_console_action", "user": user, "action": "delete_post", "slug": slug})
    return {"deleted": slug}


def upload_image(gcp, user: str, slug: str, data: bytes, content_type: str, admin: bool = False) -> dict:
    """An image for `slug`, which whoever may edit it uploads; returns the URL its Markdown links."""
    meta, _, _ = _read(gcp, slug)
    if not _can_edit(user, meta, admin):
        raise PermissionError(f"{meta.get('author')} and the console's admins edit {slug}, not {user}")
    ext = IMAGE_TYPES.get(content_type.split(";")[0].strip().lower())
    if not ext:
        raise ValueError(f"an image is one of {', '.join(IMAGE_TYPES)}, not {content_type}")
    if len(data) > MAX_IMAGE_BYTES:
        raise ValueError(f"an image is at most {MAX_IMAGE_BYTES // (1024 * 1024)} MB")
    name = f"{uuid.uuid4().hex[:12]}.{ext}"
    bucket, base = _bucket_prefix()
    gcp.write_object(bucket, f"{base}assets/{slug}/{name}", data, content_type, "0")
    return {"url": f"/api/blog/assets/{slug}/{name}"}


def read_image(gcp, slug: str, name: str) -> tuple[bytes, str]:
    stem, _, ext = name.rpartition(".")
    types = {v: k for k, v in IMAGE_TYPES.items()}
    if not re.fullmatch(r"[0-9a-f]{12}", stem) or ext not in types:
        raise KeyError(f"no image {name}")
    bucket, base = _bucket_prefix()
    _post_object(base, slug)
    got = gcp.read_object(bucket, f"{base}assets/{slug}/{name}")
    if got is None:
        raise KeyError(f"no image {name}")
    return got[0], types[ext]
