# Homepage illustration: research to venue

## Current asset — 2026-11-04

- Use: homepage overview hero.
- Asset: [research-to-venue.webp](../../website/assets/research-to-venue.webp), 1536 × 1024.
- Source supplied by the project owner: `manuscript/2026-10-25-illustrated-v2/figures/fig0-Research_Workflow_Final.pdf`, page 1.
- Source PDF SHA-256: `c72d1ee3d97f0693d08269d76e86615e268b0a25893c91913efc865456fb90ff`.
- Published WebP SHA-256: `6c2db4b9e8389803e64f7a1c6f0f26ff4b56e2b718c9dd9901f9fa24010aeefb`.
- Export: render the PDF at 1536 × 1024 with Poppler, then encode lossless WebP. Decoded pixels match the rendered PNG.
- The image URL is unchanged; the homepage uses a content version query to refresh cached copies.

Render command:

```bash
pdftoppm -png -singlefile -scale-to 1536 manuscript/2026-10-25-illustrated-v2/figures/fig0-Research_Workflow_Final.pdf /tmp/wpg-fig0-research-workflow
```

## Archived imagegen prompt — 2026-11-03

The initial illustration was generated with the built-in imagegen tool using [paper-overview.png](../../website/assets/paper-overview.png) as a style reference. It was replaced by the supplied PDF export on 2026-11-04. The prompt below records that earlier version.

```text
Use case: infographic-diagram.
Asset type: finished illustration for the right side of the Where Papers Go website homepage, landscape 3:2 composition, ideally 1536 by 1024 pixels.

Primary request: Create a NEW illustration about the real-life usefulness of Where Papers Go for researchers choosing where to submit their work. Use the supplied image only as a visual STYLE reference, not as a diagram to reproduce. Replace the entire technical benchmark subject with a clear, human-centered story.

Style/medium: faithfully echo the reference's clean academic hand-drawn infographic style: dark charcoal hand-inked outlines, gently rounded shapes, small illustrated stacks of journals and manuscript pages, a friendly little pale-blue robot librarian, a pure white background, pastel sky blue, lavender, mint green, pale yellow and a small peach accent. Crisp, carefully composed, lightly shaded flat illustrations. Warm, calm and professional, not 3D, not glossy. Slightly informal but very legible hand-lettered English headings.

Composition: three spacious stages connected from left to right by two simple dark arrows. Everything stays comfortably inside the canvas with generous margins. Each stage has a pastel rounded header near its top, and one coherent large scene beneath it. The middle scene is slightly emphasized. Minimal fine print; all visible words must remain legible when the whole image is displayed at about 500 px wide.

LEFT, sky blue header: exact text "Your research". Show a young adult researcher at a small desk using a laptop, with a manuscript sheet and pencil beside them. A little thought bubble connects their research idea to their written manuscript. On the manuscript, only the exact word "Abstract", plus a few abstract grey text lines. Convey a real academic writing situation, not a laboratory experiment.

CENTER, lavender header: exact text "Where Papers Go". A small friendly robot librarian, visually related to the robot in the reference, uses a magnifying glass to sort a few pastel journals and conference cards. Below the illustration are three large clear pastel filter chips with the EXACT words "Topic", "Rank", and "Scope". Convey finding and comparing relevant venues; avoid any chat window or fabricated data.

RIGHT, mint header: exact text "Your shortlist". A tidy, tangible pair of candidate venue cards: one illustrated journal/book labeled exactly "Journal", one illustrated conference podium/people icon labeled exactly "Conference". A short comparison checklist and a hand holding a pencil suggest the researcher reviewing options and planning a submission. Small green checks mean options have been reviewed, not acceptance. The output is a shortlist, not an acceptance letter.

Text: use only these exact strings: "Your research", "Abstract", "Where Papers Go", "Topic", "Rank", "Scope", "Your shortlist", "Journal", "Conference". No other words, no long captions, no fake tiny type. Preserve correct English spelling.

Constraints: keep the reference's approachable scholarly cartoon style and pastel palette. Illustrate practical research-to-submission discovery with visible human context. No numeric scores, no percentages, no claimed acceptance or guaranteed publication, no benchmark results, no RRF/BM25/SciNCL algorithm blocks, no data tables, no technical pipeline, no external university or publisher logos, no watermark. Fully opaque white background. This is one polished single illustration, not a webpage screenshot.
```
