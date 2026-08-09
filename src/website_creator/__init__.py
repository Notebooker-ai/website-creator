"""website-creator: an Open Notebook creator that turns notebook content into a
multi-page **static website** (emitted as ``website.v1``), rendered by Quarto to
a deployable ``_site/`` bundle shipped as a zip.

An outline pass plans the pages (pinned to the sources that inform them), a
per-page pass writes each page's markdown, and the assembled Quarto website
project (``_quarto.yml`` + ``.qmd`` pages with navbar, sidebar, and search) is
rendered with the ``quarto`` CLI already present in the creation image. The host
can then optionally publish the extracted site to the user's public storage.
"""

from __future__ import annotations

import asyncio
import json
import re
import shutil
import zipfile
from importlib import resources
from pathlib import Path
from typing import Any, ClassVar, Dict, List, Literal, Optional

from ai_prompter import Prompter
from loguru import logger
from open_notebook_creator_sdk import (
    BaseCreator,
    CreationError,
    CreationFile,
    CreationRequest,
    CreationResult,
    CreatorManifest,
    ModelRoleSpec,
)
from open_notebook_creator_sdk.schemas.website_v1 import WebsitePage, WebsiteV1
from pydantic import BaseModel, Field

__version__ = "0.1.1"

SCHEMA_ID = "website.v1"
_MAX_CONCURRENT_PAGES = 4
_RENDER_TIMEOUT_S = 600
Theme = Literal[
    "cosmo", "flatly", "litera", "journal", "minty", "pulse",
    "sandstone", "zephyr", "darkly", "cyborg",
]
_THEMES = set(Theme.__args__)


class WebsiteConfig(BaseModel):
    """Per-generation config; drives the host's generate form."""

    num_pages: int = Field(
        default=6, ge=2, le=15, description="How many pages (including the home page)"
    )
    theme: Theme = Field(
        default="cosmo",
        description="Bootswatch theme for the site's look and feel",
    )
    include_search: bool = Field(default=True, description="Built-in site search")
    include_sidebar: bool = Field(
        default=True, description="Collapsible sidebar listing every page"
    )


def _strip_fences(text: str) -> str:
    text = text.strip()
    if text.startswith("```"):
        text = re.sub(r"^```[a-zA-Z]*\n", "", text)
        text = re.sub(r"\n```$", "", text)
    return text.strip()


def _read_prompt(name: str) -> str:
    return resources.files("website_creator.prompts").joinpath(name).read_text()


def _parse_json(raw: str) -> Optional[Any]:
    try:
        return json.loads(_strip_fences(raw))
    except json.JSONDecodeError:
        return None


def _slugify(text: str) -> str:
    s = re.sub(r"[^a-z0-9]+", "-", text.lower()).strip("-")
    return s[:60] or "page"


def _valid_source_ids(value: Any, known: set) -> List[str]:
    if not isinstance(value, list):
        return []
    ids = [str(v).strip() for v in value if str(v).strip()][:8]
    if not known:
        return ids
    return [i for i in ids if i in known]


def _quarto_yml(title: str, cfg: WebsiteConfig, pages: List[Dict[str, Any]]) -> str:
    """Build _quarto.yml. Titles are LLM text: emit via json.dumps so quoting
    is always YAML-safe (JSON strings are valid YAML scalars)."""
    lines = [
        "project:",
        "  type: website",
        "",
        "website:",
        f"  title: {json.dumps(title)}",
        "  navbar:",
        "    left:",
        f"      - text: {json.dumps('Home')}",
        "        href: index.qmd",
    ]
    for p in pages[1:]:
        lines.append(f"      - text: {json.dumps(p['title'])}")
        lines.append(f"        href: {p['slug']}.qmd")
    if cfg.include_search:
        lines += ["  search: true"]
    if cfg.include_sidebar:
        lines += [
            "  sidebar:",
            "    style: docked",
            "    contents:",
            "      - index.qmd",
        ]
        for p in pages[1:]:
            lines.append(f"      - {p['slug']}.qmd")
    theme = cfg.theme if cfg.theme in _THEMES else "cosmo"
    lines += [
        "",
        "format:",
        "  html:",
        f"    theme: {theme}",
        "    toc: true",
    ]
    return "\n".join(lines) + "\n"


async def _quarto_render(project_dir: Path) -> None:
    proc = await asyncio.create_subprocess_exec(
        "quarto",
        "render",
        str(project_dir),
        stdout=asyncio.subprocess.PIPE,
        stderr=asyncio.subprocess.PIPE,
    )
    try:
        _, stderr = await asyncio.wait_for(proc.communicate(), timeout=_RENDER_TIMEOUT_S)
    except asyncio.TimeoutError:
        proc.kill()
        raise RuntimeError(f"quarto render timed out after {_RENDER_TIMEOUT_S}s")
    if proc.returncode != 0:
        detail = (stderr or b"").decode(errors="replace")
        raise RuntimeError(detail[-2000:] or f"quarto exited {proc.returncode}")


class WebsiteCreator(BaseCreator):
    config_model: ClassVar[type] = WebsiteConfig

    @property
    def manifest(self) -> CreatorManifest:
        return self.build_manifest(
            key="websites",
            name="Website",
            version=__version__,
            description=(
                "A multi-page static website built from your sources — navbar, "
                "sidebar, search — rendered by Quarto and ready to deploy or "
                "publish from your storage."
            ),
            sdk_compat=">=0.7,<1",
            emits=[SCHEMA_ID],
            model_roles=[
                ModelRoleSpec(
                    key="text",
                    kind="language",
                    requires=["structured_json"],
                    description="LLM that outlines the site and writes each page.",
                )
            ],
            icon="globe",
            suggestion_hint=(
                "the site structure: which sections or pages to create and what each "
                "should communicate"
            ),
        )

    async def generate(self, request: CreationRequest) -> CreationResult:
        cfg = WebsiteConfig.model_validate(request.config)
        role = request.models.get("text")
        if role is None:
            return CreationResult(
                status="FAILURE",
                schema_id=SCHEMA_ID,
                data={},
                errors=[CreationError(phase="setup", message="missing 'text' model role")],
                user_message="No language model was provided for the website creator.",
            )

        known_sources = {
            str(s.get("id"))
            for s in (request.content.sources or [])
            if isinstance(s, dict) and s.get("id")
        }

        # ---- outline ------------------------------------------------------
        outline_prompt = Prompter(template_text=_read_prompt("outline.jinja")).render(
            {
                "content": request.content.text,
                "sources": request.content.sources,
                "num_pages": cfg.num_pages,
                "language": request.language,
                "instructions": request.instructions,
            }
        )
        llm = role.create_language(structured={"type": "json"}, max_tokens=3000)
        resp = await llm.ainvoke(outline_prompt)
        raw = resp.content if hasattr(resp, "content") else str(resp)
        outline = _parse_json(raw)
        planned = outline.get("pages") if isinstance(outline, dict) else None
        if not isinstance(planned, list) or not planned:
            return CreationResult(
                status="FAILURE",
                schema_id=SCHEMA_ID,
                data={},
                errors=[
                    CreationError(
                        phase="plan", message="outline returned no pages", retryable=True
                    )
                ],
                user_message="The model could not outline a website from this content. Please retry.",
            )

        site_title = str(outline.get("title") or "").strip() or "Notebook Site"
        description = str(outline.get("description") or "").strip() or None

        pages: List[Dict[str, Any]] = []
        seen_slugs: set = set()
        for item in planned[: cfg.num_pages]:
            if not isinstance(item, dict):
                continue
            title = str(item.get("title") or "").strip()
            if not title:
                continue
            slug = _slugify(str(item.get("slug") or title))
            while slug in seen_slugs:
                slug = f"{slug}-{len(seen_slugs)}"
            seen_slugs.add(slug)
            pages.append(
                {
                    "title": title,
                    "slug": slug,
                    "summary": str(item.get("summary") or "").strip(),
                    "source_ids": _valid_source_ids(item.get("source_ids"), known_sources),
                }
            )
        if not pages:
            return CreationResult(
                status="FAILURE",
                schema_id=SCHEMA_ID,
                data={},
                errors=[CreationError(phase="plan", message="no valid pages in outline")],
                user_message="No website pages could be planned from this content.",
            )

        # ---- write pages (bounded fan-out) --------------------------------
        page_template = _read_prompt("page.jinja")
        sem = asyncio.Semaphore(_MAX_CONCURRENT_PAGES)

        async def write_page(p: Dict[str, Any], is_home: bool) -> Optional[str]:
            prompt = Prompter(template_text=page_template).render(
                {
                    "content": request.content.text,
                    "page": p,
                    "site_title": site_title,
                    "all_pages": pages,
                    "is_home": is_home,
                    "language": request.language,
                    "instructions": request.instructions,
                }
            )
            async with sem:
                try:
                    p_llm = role.create_language(max_tokens=3500)
                    p_resp = await p_llm.ainvoke(prompt)
                except Exception as e:  # noqa: BLE001
                    logger.warning(f"website: page '{p['title']}' failed: {e}")
                    return None
            body = p_resp.content if hasattr(p_resp, "content") else str(p_resp)
            body = _strip_fences(body)
            return body if body.strip() else None

        bodies = await asyncio.gather(
            *(write_page(p, i == 0) for i, p in enumerate(pages))
        )
        kept: List[Dict[str, Any]] = []
        kept_bodies: List[str] = []
        for p, b in zip(pages, bodies):
            if b is not None:
                kept.append(p)
                kept_bodies.append(b)
        dropped = len(pages) - len(kept)
        if not kept:
            return CreationResult(
                status="FAILURE",
                schema_id=SCHEMA_ID,
                data={},
                errors=[
                    CreationError(
                        phase="generate", message="no pages produced", retryable=True
                    )
                ],
                user_message="No website pages could be written. Please retry.",
            )
        pages = kept

        # ---- assemble the Quarto project ----------------------------------
        output_dir = Path(request.output_dir)
        project_dir = output_dir / "site-src"
        project_dir.mkdir(parents=True, exist_ok=True)
        (project_dir / "_quarto.yml").write_text(
            _quarto_yml(site_title, cfg, pages), "utf-8"
        )
        for i, (p, body) in enumerate(zip(pages, kept_bodies)):
            fname = "index.qmd" if i == 0 else f"{p['slug']}.qmd"
            front = f"---\ntitle: {json.dumps(p['title'])}\n---\n\n"
            (project_dir / fname).write_text(front + body + "\n", "utf-8")

        data = WebsiteV1(
            title=site_title,
            description=description,
            theme=cfg.theme if cfg.theme in _THEMES else "cosmo",
            pages=[
                WebsitePage(
                    title=p["title"],
                    slug=("index" if i == 0 else p["slug"]),
                    summary=p["summary"] or None,
                    source_ids=p["source_ids"],
                )
                for i, p in enumerate(pages)
            ],
        ).model_dump()

        warnings: List[str] = []
        errors: List[CreationError] = []
        if dropped:
            warnings.append(f"{dropped} planned page(s) could not be written and were skipped.")

        # ---- render + zip -------------------------------------------------
        files: List[CreationFile] = []
        try:
            await _quarto_render(project_dir)
            site_dir = project_dir / "_site"
            if not (site_dir / "index.html").is_file():
                raise RuntimeError("quarto reported success but produced no index.html")
            zip_rel = "website.zip"
            zip_path = output_dir / zip_rel

            def _zip() -> None:
                with zipfile.ZipFile(zip_path, "w", zipfile.ZIP_DEFLATED) as zf:
                    for f in sorted(site_dir.rglob("*")):
                        if f.is_file():
                            zf.write(f, f.relative_to(site_dir))

            await asyncio.to_thread(_zip)
            files.append(
                CreationFile(
                    filename=f"{_slugify(site_title)}-website.zip",
                    content_type="application/zip",
                    path=zip_rel,
                    label="site",
                )
            )
            shutil.rmtree(project_dir, ignore_errors=True)
        except FileNotFoundError:
            logger.error("website: 'quarto' binary not found on PATH")
            errors.append(CreationError(phase="render", message="quarto not installed"))
        except Exception as e:  # noqa: BLE001
            logger.error(f"website: render failed: {e}")
            errors.append(CreationError(phase="render", message=str(e)))

        if not files:
            return CreationResult(
                status="FAILURE",
                schema_id=SCHEMA_ID,
                data=data,
                errors=errors or [CreationError(phase="render", message="site not rendered")],
                user_message="The website could not be rendered.",
            )

        return CreationResult(
            status="SUCCESS",
            schema_id=SCHEMA_ID,
            data=data,
            files=files,
            warnings=warnings,
            errors=errors,
        )
