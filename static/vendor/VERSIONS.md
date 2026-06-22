# Vendored third-party browser libraries

These libraries are vendored LOCALLY (no CDN) on purpose: the Review workstation
renders confidential legal documents in the browser, and those bytes must never
leave the client. A CDN dependency would be an offline/CSP/privacy liability, so
every byte that touches a counterparty NDA is served from our own origin.

| Library      | Version | License                       | File                                  |
|--------------|---------|-------------------------------|---------------------------------------|
| docx-preview | 0.3.7   | Apache-2.0                    | `docx-preview/docx-preview.min.js`    |
| JSZip        | 3.10.1  | MIT (dual MIT / GPL-3.0; we elect MIT) | `jszip/jszip.min.js`         |

## Provenance

Both files are the unmodified UMD/minified browser builds published to npm:

- docx-preview: `npm pack docx-preview@0.3.7` -> `dist/docx-preview.min.js`
  - upstream: https://github.com/VolodymyrBaydalka/docxjs
  - sha256(docx-preview.min.js) = a011a499016a269eb048b8558a3eefc94bc33568ef434235943948ff24a40005
- JSZip: `npm pack jszip@3.10.1` -> `dist/jszip.min.js`
  - upstream: https://github.com/Stuk/jszip
  - sha256(jszip.min.js) = acc7e41455a80765b5fd9c7ee1b8078a6d160bbbca455aeae854de65c947d59e

Full license texts are kept alongside each library (`docx-preview/LICENSE`,
`jszip/LICENSE.markdown`).

## Load order (important)

`docx-preview` is a UMD bundle. Loaded as a plain `<script>` (no AMD/CommonJS
loader) it reads the **global** `JSZip` and assigns `window.docx`. JSZip must
therefore be loaded BEFORE docx-preview:

```html
<script src="/static/vendor/jszip/jszip.min.js"></script>
<script src="/static/vendor/docx-preview/docx-preview.min.js"></script>
```

After both load, the browser globals are `window.JSZip` and `window.docx`
(with `window.docx.renderAsync(blob, bodyContainer, styleContainer, options)`).

## Tracked changes

`renderAsync` only renders Word tracked changes (`w:ins` / `w:del`) when the
option `renderChanges: true` is passed (it defaults to `false`). With it on,
inserted runs render as `<ins>` elements and deleted runs as `<del>` elements.
This is validated headlessly in `tests/frontend/docx-faithful-render.mjs`.
