"""Structural guards so a shipped module can't drift out from under its tests.

"Green" only means "shipped works" if the thing the tests drive is the thing
production ships. These assertions are deliberately structural (no behavior):
they fail fast the moment the tested surface and the shipped surface diverge —
e.g. a new repository method gets added to one adapter but not the other, or a
service quietly starts defaulting to the in-memory double instead of the disk
store.

Pairs with test_matter_repository_contract.py, which runs the *behavioral*
contract over both adapters. Together: the behavior is verified on the shipped
adapter, and this file proves the shipped adapter is still the one wired in.
"""
from __future__ import annotations

import inspect

from nda_automation import (
    annotated_pdf_export,
    ingestion_service,
    redline_export_service,
)
from nda_automation.matter_repository import (
    DiskMatterRepository,
    InMemoryMatterRepository,
    MatterRepository,
)


def _public_methods(obj) -> set[str]:
    return {
        name
        for name in dir(obj)
        if not name.startswith("_") and callable(getattr(obj, name))
    }


def test_both_adapters_satisfy_protocol():
    assert isinstance(DiskMatterRepository(), MatterRepository)
    assert isinstance(InMemoryMatterRepository(), MatterRepository)


def test_protocol_methods_exist_on_both_adapters():
    """Every operation declared on the seam exists on BOTH adapters.

    If a security/data fix adds a method to one adapter, this fails until the
    other gains it too, so neither the shipped store nor the test double can
    silently lack a guarded operation.
    """
    protocol_ops = {
        name
        for name in vars(MatterRepository)
        if not name.startswith("_")
    }
    disk_ops = _public_methods(DiskMatterRepository())
    mem_ops = _public_methods(InMemoryMatterRepository())

    missing_on_disk = protocol_ops - disk_ops
    missing_on_mem = protocol_ops - mem_ops
    assert not missing_on_disk, f"DiskMatterRepository missing: {sorted(missing_on_disk)}"
    assert not missing_on_mem, f"InMemoryMatterRepository missing: {sorted(missing_on_mem)}"


def test_adapter_method_surfaces_match():
    """The two adapters expose the same public method surface.

    A method present on only one adapter is a coverage hole: the contract suite
    parametrized over both would exercise it on one path and skip it on the
    other. Keeping the surfaces identical means the contract tests cover the
    same operations on the shipped and double paths.
    """
    disk_ops = _public_methods(DiskMatterRepository())
    mem_ops = _public_methods(InMemoryMatterRepository())
    assert disk_ops == mem_ops, (
        "Adapter method surfaces diverged: "
        f"only on disk={sorted(disk_ops - mem_ops)}, "
        f"only on in_memory={sorted(mem_ops - disk_ops)}"
    )


def test_adapter_signatures_match():
    """Shared operations have identical signatures on both adapters.

    Prevents a fix that adds/renames a parameter (e.g. an owner_user_id scoping
    arg) on one adapter while the other keeps the old shape — which would let a
    caller's security argument be silently ignored on one path.
    """
    disk = DiskMatterRepository()
    mem = InMemoryMatterRepository()
    shared = _public_methods(disk) & _public_methods(mem)
    mismatches = {}
    for name in sorted(shared):
        disk_sig = inspect.signature(getattr(disk, name))
        mem_sig = inspect.signature(getattr(mem, name))
        if disk_sig != mem_sig:
            mismatches[name] = (str(disk_sig), str(mem_sig))
    assert not mismatches, f"Adapter signature drift: {mismatches}"


def test_disk_adapter_delegates_to_shipped_matter_store():
    """The disk adapter is a thin shim over the shipped matter_store module.

    Asserts the adapter holds no orchestration of its own — every disk method
    calls the same-named matter_store function — so behavior verified through
    DiskMatterRepository really is matter_store's behavior.
    """
    disk = DiskMatterRepository()
    for name in _public_methods(disk):
        source = inspect.getsource(getattr(DiskMatterRepository, name))
        assert f"matter_store.{name}(" in source, (
            f"DiskMatterRepository.{name} does not delegate to matter_store.{name}; "
            "the shipped store and its tested adapter have diverged."
        )


def test_production_services_default_to_the_shipped_disk_adapter():
    """Services must default to DiskMatterRepository, not the in-memory double.

    The double is a test fast path; if a service's default flips to it, the
    shipped path stops being the real default and the gate would validate the
    double. This pins the default to the shipped adapter.
    """
    services = [
        redline_export_service,
        annotated_pdf_export,
        ingestion_service,
    ]
    for module in services:
        source = inspect.getsource(module)
        assert "DiskMatterRepository()" in source, (
            f"{module.__name__} no longer defaults to DiskMatterRepository; "
            "production may be running on the in-memory test double."
        )
        assert "InMemoryMatterRepository" not in source, (
            f"{module.__name__} references the in-memory test double in shipped code."
        )


def test_in_memory_double_reuses_matter_store_pure_helpers():
    """The double reuses matter_store's pure helpers rather than reimplementing.

    Matter shapes, owner scoping, pruning and gmail de-dup must be the SAME
    logic on both paths. Importing them from matter_store (not redefining them)
    is what keeps the double honest; this guard fails if the double stops doing
    so for the security-critical helpers.
    """
    source = inspect.getsource(InMemoryMatterRepository)
    for helper in ("_matter_owner_matches", "_prune_stored_matters", "_intake_metadata"):
        assert helper in source, (
            f"InMemoryMatterRepository no longer uses matter_store.{helper}; "
            "the double may have reimplemented security-critical logic."
        )
        # And it must be the imported one, not a locally redefined function.
        assert f"def {helper}(" not in source, (
            f"InMemoryMatterRepository redefines {helper} instead of reusing "
            "matter_store's; the shared-helper guarantee is broken."
        )
