`inline_diff_vectors.json` is a generated golden fixture shared by the Python
DOCX export tests and the browser inline-diff tests.

Edit `inline_diff_vectors.source.json`, then run:

```sh
node tests/fixtures/generate_inline_diff_vectors.mjs
```

Do not hand-edit the materialized large fallback vector in
`inline_diff_vectors.json`.
