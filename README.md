# website-creator

An [Open Notebook](https://github.com/Notebooker-ai/open-notebook-nb) creator that
turns a notebook's sources into a **multi-page static website** (`website.v1`) —
navbar, sidebar, built-in search — rendered by [Quarto](https://quarto.org/) and
shipped as a deployable `_site/` zip.

## How it works

1. An **outline pass** plans the site: a home page plus topic pages, each pinned
   to the source ids that inform it.
2. A **per-page pass** (bounded fan-out) writes each page in Quarto markdown,
   cross-linking related pages.
3. The assembled project (`_quarto.yml` + `.qmd` pages) renders with the
   `quarto` CLI already in the creation image; the `_site/` output ships as
   `website.zip` — drop it on any static host, or use the host app's
   **publish** endpoint to push it to your configured S3/R2 storage and get a
   public URL.

## Config

| field | default | notes |
| --- | --- | --- |
| `num_pages` | 6 | 2–15, including the home page |
| `theme` | `cosmo` | Bootswatch: cosmo, flatly, litera, journal, minty, pulse, sandstone, zephyr, darkly, cyborg |
| `include_search` | `true` | Quarto's built-in site search |
| `include_sidebar` | `true` | Docked sidebar listing every page |

## Development

```bash
uv sync --extra dev
uv run pytest
```

Tests stub the language model; render tests skip when `quarto` isn't installed
(the project files are asserted either way).
