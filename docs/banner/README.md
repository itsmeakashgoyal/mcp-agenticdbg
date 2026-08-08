# Regenerating `docs/banner.png`

`banner.html` is a self-contained HTML file (Adobe Fonts embed + inline
CSS, no build step) rendered to a 1280×640 PNG — the exact size GitHub
recommends for a repo's social-preview image, and also used as the
README hero.

Fonts: [Neighbor](https://fonts.adobe.com/fonts/neighbor) (display),
[Source Sans 3](https://fonts.adobe.com/fonts/source-sans-3) (body), and
[Ubuntu Mono](https://fonts.adobe.com/fonts/ubuntu-mono) (the terminal
mockup), via an Adobe Fonts kit.

## Regenerate

Needs Chrome/Chromium (for a pixel-accurate headless screenshot — the
`--virtual-time-budget` flag matters, since Adobe Fonts loads over the
network and a bare `--screenshot` fires before the webfonts arrive,
capturing invisible text):

```bash
cd docs/banner
python3 -m http.server 8934 &
"/Applications/Google Chrome.app/Contents/MacOS/Google Chrome" \
  --headless --disable-gpu --hide-scrollbars \
  --window-size=1280,640 --virtual-time-budget=5000 \
  --screenshot=../banner.png \
  http://localhost:8934/banner.html
kill %1
```

## Setting it as the GitHub social-preview image

There's no API for this — upload manually:
**Settings → General → Social preview → Edit → upload `docs/banner.png`.**

## Editing

The terminal mockup's content is a trimmed, real excerpt from
`docs/demos/src/use-after-free/analysis.md` — keep it consistent with
the actual demo GIFs if you change either one.
