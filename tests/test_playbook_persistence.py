"""#39: the published playbook must live on the NDA_DATA_DIR persistent disk.

``checker.PLAYBOOK_PATH`` used to be the repo root, so ``playbook.json`` + its
``.runtime`` / ``.draft`` / ``.history`` sidecars lived in the disposable
deployed image and a redeploy silently reverted any prod publish + version
history. These tests pin the persistent-resolution contract:

* dev (no NDA_DATA_DIR) reads stay on the bundled in-repo copy;
* with NDA_DATA_DIR set, the live path is under the persistent disk and is
  SEEDED from the bundled copy on first run when absent;
* a redeploy with NEW bundled bytes does NOT clobber a published persistent copy
  (the publish survives the "redeploy").
"""

from __future__ import annotations

import json
from pathlib import Path

from nda_automation import checker as checker_module
from nda_automation import playbook_runtime


def _resolve(monkeypatch, data_dir: Path | None) -> Path:
    if data_dir is None:
        monkeypatch.delenv("NDA_DATA_DIR", raising=False)
    else:
        monkeypatch.setenv("NDA_DATA_DIR", str(data_dir))
    return checker_module._resolve_playbook_path()


def test_dev_without_data_dir_uses_bundled_copy(monkeypatch) -> None:
    resolved = _resolve(monkeypatch, None)
    assert resolved == checker_module.BUNDLED_PLAYBOOK_PATH
    # Reads still work (load_playbook reads PLAYBOOK_PATH which == bundled in dev).
    assert checker_module.BUNDLED_PLAYBOOK_PATH.exists()


def test_data_dir_seeds_from_bundled_on_first_run(monkeypatch, tmp_path) -> None:
    data_dir = tmp_path / "var-data"
    persistent = data_dir / "playbook.json"
    assert not persistent.exists()

    resolved = _resolve(monkeypatch, data_dir)

    # The live path is the persistent copy, seeded with the bundled bytes.
    assert resolved == persistent
    assert persistent.exists()
    assert json.loads(persistent.read_text(encoding="utf-8")) == json.loads(
        checker_module.BUNDLED_PLAYBOOK_PATH.read_text(encoding="utf-8")
    )


def test_sidecars_derive_under_the_persistent_disk(monkeypatch, tmp_path) -> None:
    data_dir = tmp_path / "var-data"
    resolved = _resolve(monkeypatch, data_dir)

    # The runtime/draft/history sidecars all derive from the resolved path, so they
    # land on the persistent disk too (the .with_name() relationship).
    for path_for in (
        playbook_runtime.runtime_path_for,
        playbook_runtime.draft_path_for,
        playbook_runtime.history_path_for,
    ):
        assert path_for(resolved).parent == data_dir


def test_redeploy_does_not_clobber_a_published_playbook(monkeypatch, tmp_path) -> None:
    data_dir = tmp_path / "var-data"

    # First boot: seed the persistent copy.
    persistent = _resolve(monkeypatch, data_dir)
    assert persistent.exists()

    # Simulate a PROD PUBLISH landing on the persistent disk.
    published = {"clauses": [], "_published_marker": "prod-publish-v2"}
    persistent.write_text(json.dumps(published), encoding="utf-8")

    # Simulate a REDEPLOY: a new process boots, resolves again. Even if the bundled
    # image shipped DIFFERENT bytes, the published persistent copy must survive
    # (the resolver only seeds when the persistent copy is ABSENT).
    resolved_again = _resolve(monkeypatch, data_dir)
    assert resolved_again == persistent
    assert json.loads(persistent.read_text(encoding="utf-8")) == published


def test_unwritable_data_dir_falls_back_to_bundled(monkeypatch, tmp_path) -> None:
    # If the persistent disk cannot be written, resolution must not crash the
    # process -- reads fall back to the bundled copy.
    data_dir = tmp_path / "var-data"

    def _boom(*args, **kwargs):
        raise OSError("read-only disk")

    monkeypatch.setattr(checker_module.shutil, "copy2", _boom)
    resolved = _resolve(monkeypatch, data_dir)
    assert resolved == checker_module.BUNDLED_PLAYBOOK_PATH
