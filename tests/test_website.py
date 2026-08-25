"""Tests for WebsiteCreator using a stubbed language model (no network).

The quarto render step runs for real when the `quarto` binary is available;
otherwise the tests still assert the full project (_quarto.yml + .qmd pages)
was written and the result reports the render failure honestly.
"""

from __future__ import annotations

import json
import shutil
import tempfile
from pathlib import Path

import pytest
from open_notebook_creator_sdk import ContentBundle, CreationRequest, ModelRole
from open_notebook_creator_sdk.testing import assert_creator_compliant
from website_creator import WebsiteCreator, _quarto_yml, _slugify, WebsiteConfig

HAS_QUARTO = shutil.which("quarto") is not None


class _FakeResp:
    def __init__(self, content: str):
        self.content = content


class _FakeLLM:
    def __init__(self, payload: str):
        self._payload = payload

    async def ainvoke(self, _prompt):
        return _FakeResp(self._payload)


class _QueueRole(ModelRole):
    """First create_language call returns payloads[0] (the outline); later
    calls cycle through the rest (page bodies, last one repeating)."""

    payloads: list = []
    calls: int = 0

    def create_language(self, **_):
        i = min(self.calls, len(self.payloads) - 1)
        self.calls += 1
        return _FakeLLM(self.payloads[i])


def _outline(pages):
    return json.dumps(
        {"title": "Test Site", "description": "A site about tests.", "pages": pages}
    )


_PAGES = [
    {"title": "Welcome", "slug": "welcome", "summary": "overview", "source_ids": ["source:a"]},
    {"title": "Deep Topic", "slug": "deep-topic", "summary": "details", "source_ids": ["source:a"]},
]

_BODY = "## Section\n\nGrounded content with a [link](deep-topic.html).\n"


def _request(td, payloads, config=None):
    return CreationRequest(
        content=ContentBundle(
            text="Some source material about tests.",
            sources=[{"id": "source:a", "title": "Source A"}],
        ),
        config=config or {"num_pages": 2},
        models={"text": _QueueRole(provider="fake", model="fake", payloads=payloads)},
        output_dir=td,
        artifact_id="art-1",
    )


def test_static_compliance():
    assert_creator_compliant(WebsiteCreator())


def test_slugify():
    assert _slugify("Hello, World!") == "hello-world"
    assert _slugify("___") == "page"


def test_quarto_yml_quotes_llm_titles_safely():
    cfg = WebsiteConfig()
    yml = _quarto_yml(
        'Evil: "quotes" & colons',
        cfg,
        [{"title": "A: b", "slug": "index"}, {"title": 'X "y"', "slug": "x"}],
    )
    # JSON-quoted scalars are valid YAML and neutralize colons/quotes
    assert '"Evil: \\"quotes\\" & colons"' in yml
    assert "search: true" in yml
    assert "theme: cosmo" in yml


@pytest.mark.asyncio
async def test_generate_writes_full_project():
    creator = WebsiteCreator()
    with tempfile.TemporaryDirectory() as td:
        result = await creator.generate(_request(td, [_outline(_PAGES), _BODY, _BODY]))
        src = Path(td) / "site-src"
        if HAS_QUARTO:
            assert result.status == "SUCCESS"
            assert result.files[0].content_type == "application/zip"
            assert (Path(td) / "website.zip").exists()
        else:
            # honest failure, but the project was fully assembled first
            assert result.status == "FAILURE"
            assert result.errors[0].phase == "render"
            assert (src / "_quarto.yml").exists()
            assert (src / "index.qmd").exists()
            assert (src / "deep-topic.qmd").exists()
            text = (src / "index.qmd").read_text()
            assert text.startswith("---\ntitle:")
        assert result.data["title"] == "Test Site"
        assert [p["slug"] for p in result.data["pages"]] == ["index", "deep-topic"]
        assert result.data["pages"][0]["source_ids"] == ["source:a"]
        assert result.data["published_url"] is None


@pytest.mark.asyncio
async def test_invalid_outline_is_failure():
    creator = WebsiteCreator()
    with tempfile.TemporaryDirectory() as td:
        result = await creator.generate(_request(td, ["not json"]))
        assert result.status == "FAILURE"
        assert result.errors[0].phase == "plan"


@pytest.mark.asyncio
async def test_no_text_role_is_failure():
    creator = WebsiteCreator()
    with tempfile.TemporaryDirectory() as td:
        req = CreationRequest(content=ContentBundle(text="x"), output_dir=td, artifact_id="a")
        result = await creator.generate(req)
        assert result.status == "FAILURE"
        assert result.errors[0].phase == "setup"


@pytest.mark.asyncio
async def test_duplicate_slugs_deduped():
    creator = WebsiteCreator()
    pages = [
        {"title": "Same", "slug": "same", "summary": "", "source_ids": []},
        {"title": "Same Again", "slug": "same", "summary": "", "source_ids": []},
    ]
    with tempfile.TemporaryDirectory() as td:
        result = await creator.generate(
            _request(td, [_outline(pages), _BODY, _BODY], config={"num_pages": 2})
        )
        slugs = [p["slug"] for p in result.data["pages"]]
        assert len(set(slugs)) == len(slugs)


def test_theme_is_an_enum_for_the_generate_form():
    """`theme` must surface as a JSON-schema enum so the host renders a
    dropdown of valid Bootswatch themes instead of a free-text field."""
    schema = WebsiteConfig.model_json_schema()
    theme_schema = schema["properties"]["theme"]
    enum = theme_schema.get("enum") or [
        s["const"] for s in theme_schema.get("anyOf", []) if "const" in s
    ]
    assert "cosmo" in enum and "darkly" in enum and len(enum) == 10

    with pytest.raises(Exception):
        WebsiteConfig.model_validate({"theme": "not-a-theme"})


@pytest.mark.asyncio
async def test_made_up_source_ids_filtered():
    creator = WebsiteCreator()
    pages = [
        {"title": "Home", "slug": "home", "summary": "", "source_ids": ["source:a", "source:FAKE"]},
        {"title": "Other", "slug": "other", "summary": "", "source_ids": []},
    ]
    with tempfile.TemporaryDirectory() as td:
        result = await creator.generate(_request(td, [_outline(pages), _BODY, _BODY]))
        assert result.data["pages"][0]["source_ids"] == ["source:a"]


@pytest.mark.skipif(not HAS_QUARTO, reason="quarto not installed")
@pytest.mark.asyncio
async def test_rendered_zip_contains_site():
    import zipfile

    creator = WebsiteCreator()
    with tempfile.TemporaryDirectory() as td:
        result = await creator.generate(_request(td, [_outline(_PAGES), _BODY, _BODY]))
        assert result.status == "SUCCESS"
        with zipfile.ZipFile(Path(td) / "website.zip") as zf:
            names = zf.namelist()
            assert "index.html" in names
            assert "deep-topic.html" in names


def test_manifest_declares_view_bundle_and_it_ships():
    """Without a view bundle the notebook panel dumped raw JSON ("No renderer
    available") and the site zip was unreachable — the view flips the host to
    plugin-view mode, which also surfaces the zip download."""
    from importlib import resources

    from website_creator import WebsiteCreator

    m = WebsiteCreator().manifest
    assert m.view is not None
    assert m.view.entry == "view/index.html"
    asset = resources.files("website_creator").joinpath(m.view.entry)
    assert asset.is_file()
    html = asset.read_text()
    assert "open-notebook:ready" in html
    assert "open-notebook:artifact" in html
    assert "website.v1" in html
    assert 'src="http' not in html  # self-contained, nothing loads remotely
