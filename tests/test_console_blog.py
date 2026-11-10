"""The console's blog over a fake bucket: slugs, who edits, save conflicts and images."""
from __future__ import annotations

import pytest
import requests

from gcpbricks.console import blog as BL

ME, YOU = "me@x.com", "you@x.com"


class FakeGcp:
    def __init__(self):
        self.objects: dict[str, tuple[bytes, str]] = {}
        self.audits = []

    def list_dir(self, bucket, prefix):
        names = [n for n in self.objects if n.startswith(prefix)]
        return [], [{"name": n, "generation": self.objects[n][1]} for n in names if "/" not in n[len(prefix):]]

    def read_object(self, bucket, obj):
        return self.objects.get(obj)

    def write_object(self, bucket, obj, data, content_type, if_generation=None):
        current = self.objects.get(obj, (b"", "0"))[1]
        if if_generation is not None and if_generation != current:
            r = requests.Response()
            r.status_code = 412
            raise requests.HTTPError(response=r)
        gen = str(int(current) + 1)
        self.objects[obj] = (data, gen)
        return gen

    def delete_object(self, bucket, obj):
        self.objects.pop(obj, None)

    def audit(self, payload):
        self.audits.append(payload)


@pytest.fixture()
def gcp():
    return FakeGcp()


def test_a_title_gives_an_ascii_slug_and_a_taken_one_a_suffix(gcp):
    assert BL.slugify("Hướng dẫn: Feature Store đầu tiên!") == "huong-dan-feature-store-dau-tien"
    assert BL.create_post(gcp, ME, "Feature Store")["slug"] == "feature-store"
    assert BL.create_post(gcp, YOU, "Feature store")["slug"] == "feature-store-2"
    with pytest.raises(ValueError):
        BL.create_post(gcp, ME, "   ")


def test_a_post_round_trips_its_front_matter_and_body(gcp):
    slug = BL.create_post(gcp, ME, "Guide")["slug"]
    gen = BL.read_post(gcp, ME, slug)["generation"]
    body = "# Hello\n\n---\n\n```mermaid\ngraph LR; a-->b\n```\n"
    BL.save_post(gcp, ME, slug, "Guide v2", ["ml", " ", "dbt"], "short", body, gen)
    post = BL.read_post(gcp, YOU, slug)
    assert (post["title"], post["tags"], post["summary"], post["body"], post["author"]) == \
           ("Guide v2", ["ml", "dbt"], "short", body, ME)
    assert post["can_edit"] is False
    assert [p["slug"] for p in BL.list_posts(gcp)] == [slug]


def test_only_the_author_or_an_admin_edits_and_deletes(gcp):
    slug = BL.create_post(gcp, ME, "Mine")["slug"]
    gen = BL.read_post(gcp, ME, slug)["generation"]
    with pytest.raises(PermissionError):
        BL.save_post(gcp, YOU, slug, "Yours", [], "", "", gen)
    with pytest.raises(PermissionError):
        BL.delete_post(gcp, YOU, slug)
    gen = BL.save_post(gcp, YOU, slug, "Fixed", [], "", "", gen, admin=True)["generation"]
    assert BL.read_post(gcp, ME, slug)["author"] == ME
    BL.upload_image(gcp, ME, slug, b"png", "image/png")
    BL.delete_post(gcp, ME, slug)
    assert gcp.objects == {}


def test_a_save_over_a_newer_one_is_refused(gcp):
    slug = BL.create_post(gcp, ME, "Race")["slug"]
    gen = BL.read_post(gcp, ME, slug)["generation"]
    BL.save_post(gcp, ME, slug, "Race", [], "", "first", gen)
    with pytest.raises(ValueError, match="changed since"):
        BL.save_post(gcp, ME, slug, "Race", [], "", "second", gen)


def test_images_are_raster_only_and_read_back_by_their_url(gcp):
    slug = BL.create_post(gcp, ME, "Pics")["slug"]
    url = BL.upload_image(gcp, ME, slug, b"\x89PNG", "image/png")["url"]
    _, _, _, _, got_slug, name = url.split("/")
    assert BL.read_image(gcp, got_slug, name) == (b"\x89PNG", "image/png")
    with pytest.raises(ValueError):
        BL.upload_image(gcp, ME, slug, b"<svg/>", "image/svg+xml")
    with pytest.raises(PermissionError):
        BL.upload_image(gcp, YOU, slug, b"x", "image/png")
    with pytest.raises(KeyError):
        BL.read_image(gcp, slug, "../posts/pics.md")


def test_a_bad_slug_is_refused(gcp):
    with pytest.raises(ValueError):
        BL.read_post(gcp, ME, "../workspace/x")
