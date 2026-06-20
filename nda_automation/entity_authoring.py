"""Authoring workflow for the signing-entity registry.

This module owns the user-visible Entities-console grammar: loading the live
registry for the admin editor, validating a proposed registry, and saving it
durably. It sits between the HTTP route adapter (:mod:`nda_automation.routes.
entities`) and the persistence layer (:mod:`nda_automation.entity_store`),
mirroring how :mod:`nda_automation.playbook_authoring` relates to the playbook
runtime.

Validation is two-layered, same discipline as the playbook editor:

* **Structural** (:func:`nda_automation.entity_registry.validate_registry`):
  every entity needs an id, legal_name, governing_law.playbook_option_id, a
  court/jurisdiction, incorporation_jurisdiction, and exactly one default
  address with at least one address line.
* **Orphan guard** (against the live playbook): every entity's
  ``governing_law.playbook_option_id`` must be a currently-approved governing-law
  option in the playbook, so an entity can never point at a law the playbook does
  not sanction. The display label is normalised to the playbook's label before
  the check, so an admin only has to pick the option id from the joined dropdown.
  This guard FAILS CLOSED on the save path: if the playbook can't be read we
  cannot prove the law is approved, so the save is REJECTED (503) rather than
  persisting a possibly-unsanctioned law — the single-source-of-truth join holds
  especially when the playbook is missing.
* **Bracket guard** on identity fields: an entity's ``legal_name`` and every
  address line are written verbatim into the NDA's structured slots, and the
  engine fills square-bracket template tokens (``[GOVERNING LAW]``). A ``[`` / ``]``
  in one of these admin-writable values is REJECTED (parity with the counterparty
  intake gate) so it can never corrupt an address into the resolved forum or DoS
  generation via the leftover-placeholder guard.

A malformed payload is rejected with a structured 400 and the on-disk store is
never touched (validation runs entirely before any write), so a bad save can
never silently corrupt the registry.
"""
from __future__ import annotations

import json
from copy import deepcopy
from typing import Any

from . import entity_registry, entity_store
from .checker import PLAYBOOK_PATH
from .playbook_runtime import read_playbook_from_path

_ALLOWED_ENTITY_KEYS = {
    "id",
    "legal_name",
    "short_name",
    "addresses",
    "governing_law",
    "jurisdiction",
    "incorporation_jurisdiction",
    "signatory",
}
_ALLOWED_ADDRESS_KEYS = {"id", "label", "lines", "country", "default"}


def _reject_bracket_identity_fields(entities: list[dict[str, Any]]) -> None:
    """Reject admin-supplied identity values that conflict with the fill markers.

    Mirrors :func:`nda_automation.nda_generation.validate_intake_identity_fields`
    (the counterparty intake gate) for the Aspora-entity side: an entity's
    ``legal_name`` and every address line are written verbatim into the NDA's
    structured identity slots, and the engine fills square-bracket template tokens
    (``[GOVERNING LAW]``, ``[COMPANY NAME]``). A ``[`` / ``]`` in one of these
    values therefore either collides with a real fill token — silently rewriting an
    address to the resolved forum (corruption) or tripping the fail-closed
    leftover-placeholder guard (a ``[GOVERNING LAW]`` legal_name DoSes generation) —
    or leaves stray bracketed text in a signed legal document. We REJECT (never
    rewrite — a name/address is a legal value) with a clear, field-scoped error.
    The first offending field is reported (one fix per attempt).
    """

    for entity in entities:
        entity_id = entity.get("id") or "(unnamed entity)"
        legal_name = str(entity.get("legal_name") or "")
        if "[" in legal_name or "]" in legal_name:
            bad = "[" if "[" in legal_name else "]"
            raise EntityAuthoringError(
                {
                    "error": (
                        f"Entity '{entity_id}' legal_name contains a square bracket "
                        f"'{bad}', which conflicts with the NDA template's fill markers "
                        f"(e.g. [GOVERNING LAW]). Remove the bracketed text and try again."
                    )
                },
                status=400,
            )
        for address in entity.get("addresses") or []:
            for line in address.get("lines") or []:
                line_text = str(line or "")
                if "[" in line_text or "]" in line_text:
                    bad = "[" if "[" in line_text else "]"
                    raise EntityAuthoringError(
                        {
                            "error": (
                                f"Entity '{entity_id}' has an address line containing a "
                                f"square bracket '{bad}', which conflicts with the NDA "
                                f"template's fill markers (e.g. [GOVERNING LAW]). Remove "
                                f"the bracketed text and try again."
                            )
                        },
                        status=400,
                    )


class EntityAuthoringError(RuntimeError):
    """Structured authoring failure for the route adapter (carries an HTTP status)."""

    def __init__(self, payload: dict[str, Any], *, status: int = 400) -> None:
        super().__init__(str(payload.get("error") or "Entity authoring failed."))
        self.payload = payload
        self.status = status


def _read_playbook_or_none() -> dict[str, Any] | None:
    try:
        return read_playbook_from_path(PLAYBOOK_PATH)
    except (OSError, json.JSONDecodeError):
        return None


def load_entities_workspace() -> dict[str, Any]:
    """Return the admin Entities-console payload: live entities + playbook law options.

    The ``governing_law_options`` ({id,label}) are sourced live from the playbook
    so the editor's law dropdown is playbook-driven (single source of truth). When
    the playbook can't be read the entities still load (the registry is
    self-contained); the dropdown then has no playbook-sourced options and the UI
    surfaces an orphan warning if any entity points at an unknown law.
    """
    playbook = _read_playbook_or_none()
    payload = entity_registry.signing_entities_payload(playbook)
    payload["playbook_available"] = playbook is not None
    return payload


def _coerce_entity(raw: Any) -> dict[str, Any]:
    """Normalise one entity object, dropping unknown keys and tidying types.

    Returns a clean dict. Raises :class:`EntityAuthoringError` for shapes that
    can never be valid (non-object entity / address). Field-level "missing
    required value" checks are left to :func:`entity_registry.validate_registry`
    so every save flows through the single validation oracle.
    """
    if not isinstance(raw, dict):
        raise EntityAuthoringError({"error": "Each entity must be an object."}, status=400)

    entity: dict[str, Any] = {}
    entity["id"] = str(raw.get("id") or "").strip()
    entity["legal_name"] = str(raw.get("legal_name") or "").strip()
    entity["short_name"] = str(raw.get("short_name") or "").strip()
    entity["jurisdiction"] = str(raw.get("jurisdiction") or "").strip()
    entity["incorporation_jurisdiction"] = str(
        raw.get("incorporation_jurisdiction") or ""
    ).strip()

    law = raw.get("governing_law")
    if not isinstance(law, dict):
        law = {}
    entity["governing_law"] = {
        "playbook_option_id": str(law.get("playbook_option_id") or "").strip(),
        "label": str(law.get("label") or "").strip(),
    }

    signatory = raw.get("signatory")
    if not isinstance(signatory, dict):
        signatory = {}
    entity["signatory"] = {
        "name": str(signatory.get("name") or "").strip(),
        "title": str(signatory.get("title") or "").strip(),
    }

    addresses_raw = raw.get("addresses")
    if not isinstance(addresses_raw, list):
        addresses_raw = []
    addresses: list[dict[str, Any]] = []
    for index, address_raw in enumerate(addresses_raw):
        if not isinstance(address_raw, dict):
            raise EntityAuthoringError(
                {"error": "Each address must be an object."}, status=400
            )
        lines_raw = address_raw.get("lines")
        if isinstance(lines_raw, str):
            lines = [line.strip() for line in lines_raw.splitlines() if line.strip()]
        elif isinstance(lines_raw, list):
            lines = [str(line).strip() for line in lines_raw if str(line).strip()]
        else:
            lines = []
        addresses.append(
            {
                "id": str(address_raw.get("id") or "").strip() or f"address_{index + 1}",
                "label": str(address_raw.get("label") or "").strip() or "Office",
                "lines": lines,
                "country": str(address_raw.get("country") or "").strip(),
                "default": bool(address_raw.get("default")),
            }
        )
    entity["addresses"] = addresses
    return entity


def _normalise_law_labels(
    entities: list[dict[str, Any]], playbook: dict[str, Any] | None
) -> None:
    """Set each entity's ``governing_law.label`` to the playbook's label for its id.

    The label is display-only (the operative law is pulled from the playbook via
    the id), so the editor only needs to pick the option id from the joined
    dropdown; we backfill the canonical label here so the registry's cached copy
    can never drift from the playbook. Entities whose id is not an approved option
    are left as-is so the orphan-guard validation can reject them with a clear
    message rather than this masking the drift.
    """
    if playbook is None:
        return
    label_by_id = {
        option["id"]: option["label"]
        for option in entity_registry._playbook_governing_law_options(playbook)
    }
    for entity in entities:
        option_id = entity.get("governing_law", {}).get("playbook_option_id", "")
        if option_id in label_by_id:
            entity["governing_law"]["label"] = label_by_id[option_id]


def save_entities_registry(
    payload: dict[str, Any],
    *,
    actor: str = "admin",
    store_path=None,
) -> dict[str, Any]:
    """Validate and durably persist a proposed signing-entity registry.

    ``payload["entities"]`` is the full replacement list (publish-style save). The
    candidate is coerced, structurally validated, and orphan-guarded against the
    live playbook BEFORE any disk write, so a rejected save is a no-op. On success
    the store is rewritten atomically and the fresh workspace payload is returned.

    ``store_path`` resolves at call time from ``entity_store.ENTITY_STORE_PATH``
    when not supplied, so a test (or a relocated data dir) is honoured.
    """
    if store_path is None:
        store_path = entity_store.ENTITY_STORE_PATH
    entities_raw = payload.get("entities")
    if not isinstance(entities_raw, list):
        raise EntityAuthoringError(
            {"error": "Provide an 'entities' list to save."}, status=400
        )

    entities = [_coerce_entity(raw) for raw in entities_raw]

    # Bracket guard on admin-writable identity fields (legal_name + address lines),
    # parity with the counterparty intake gate: a '[' / ']' would collide with the
    # engine's fill tokens (address corruption / generation DoS). Reject before any
    # write.
    _reject_bracket_identity_fields(entities)

    playbook = _read_playbook_or_none()
    _normalise_law_labels(entities, playbook)

    # FAIL-CLOSED: the orphan guard (every governing-law id must be a currently
    # approved playbook option) is the single-source-of-truth join and must hold
    # ESPECIALLY when the playbook is missing. If the playbook can't be read we
    # cannot prove the law is approved, so we REJECT the save rather than skipping
    # the guard and persisting an entity that may point at an unsanctioned law.
    if playbook is None:
        raise EntityAuthoringError(
            {
                "error": (
                    "The governing-law playbook could not be read, so signing-entity "
                    "law options cannot be validated against it. The registry was not "
                    "saved. Restore the playbook and try again."
                )
            },
            status=503,
        )

    # Structural validation first (id/name/law-id/court/address invariants), then
    # the orphan guard against the live playbook (the law must be approved). Both
    # raise ValueError; surface as a structured 400 so a bad save never corrupts
    # the store.
    try:
        entity_registry.validate_registry(entities)
        entity_registry.validate_registry_against_playbook(playbook, entities)
    except ValueError as error:
        raise EntityAuthoringError({"error": str(error)}, status=400) from error

    try:
        entity_store.save_entities(entities, store_path=store_path, actor=actor)
    except OSError as error:
        raise EntityAuthoringError(
            {"error": "Signing-entity registry could not be saved."}, status=500
        ) from error

    workspace = load_entities_workspace()
    workspace["saved"] = True
    return workspace


def validate_entities_payload(payload: dict[str, Any]) -> dict[str, Any]:
    """Validate a proposed registry without persisting it (the editor's preview gate).

    Returns ``{"valid", "errors"}``. ``errors`` is a list of human-readable
    strings (structural + orphan-guard). This never writes to disk.
    """
    entities_raw = payload.get("entities")
    if not isinstance(entities_raw, list):
        return {"valid": False, "errors": ["Provide an 'entities' list to validate."]}

    errors: list[str] = []
    try:
        entities = [_coerce_entity(raw) for raw in entities_raw]
    except EntityAuthoringError as error:
        return {"valid": False, "errors": [str(error)]}

    # Surface the bracket-identity-field rejection (C2) in the preview gate too, so
    # the editor flags it before a save attempt rather than only at persist time.
    try:
        _reject_bracket_identity_fields(entities)
    except EntityAuthoringError as error:
        errors.append(str(error))

    playbook = _read_playbook_or_none()
    _normalise_law_labels(entities, playbook)
    try:
        entity_registry.validate_registry(entities)
        if playbook is not None:
            entity_registry.validate_registry_against_playbook(playbook, entities)
    except ValueError as error:
        errors.append(str(error))

    return {"valid": not errors, "errors": errors}


__all__ = [
    "EntityAuthoringError",
    "load_entities_workspace",
    "save_entities_registry",
    "validate_entities_payload",
]
