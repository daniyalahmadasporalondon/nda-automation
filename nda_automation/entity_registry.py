"""Registry of Aspora's signing entities.

Each signing entity is a self-contained *bundle* so the draft flow can pick one
and have the legal name, address, and governing-law clause travel together.

The ``governing_law.playbook_option_id`` on every bundle is the join key into the
live playbook's ``governing_law`` clause (``rules.approved_options[].id``). Picking
an entity therefore selects the matching approved governing-law option, so the
right clause wording is pulled automatically when an NDA is generated.

The four positions exposed by the playbook today are ``india``, ``delaware``,
``england_and_wales`` and ``difc``. :func:`validate_registry_against_playbook`
checks that every bundle maps to one of those positions and fails loudly if the
playbook drifts (e.g. an option id is renamed), rather than silently generating
an unapproved governing law.

The bundles below (:data:`DEFAULT_SIGNING_ENTITIES`) are the *seed* defaults. The
live registry is now authorable and durable: it is read from a persistent JSON
store under ``$NDA_DATA_DIR`` (see :mod:`nda_automation.entity_store`), seeded
once from these defaults so nothing breaks on first run. The accessors
(:func:`list_entities`, :func:`get_entity`, ...) read the *live* store, so an
admin edit in the Entities console is reflected everywhere the registry is
consumed (the Generator's signing-entity picker, generation, DocuSign, etc.).
"""

from __future__ import annotations

from typing import Any, Mapping

# ---------------------------------------------------------------------------
# Entity bundles
# ---------------------------------------------------------------------------
#
# Address shape: {id, label, lines[], country, default}. Exactly one address per
# entity is the default. Real Transfer Limited carries two (a registered office
# in Belfast and a corporate office in London); the London corporate office is
# the default for an NDA because the matching playbook governing-law position is
# England and Wales (see the flag in ENTITY_LAW_MAPPING_NOTES below).
#
# governing_law.playbook_option_id MUST equal a governing_law approved_options id
# in playbook.json. Keep these in sync; validate_registry_against_playbook()
# enforces it.

DEFAULT_SIGNING_ENTITIES: list[dict[str, Any]] = [
    {
        "id": "aspora_technology",
        "legal_name": "Aspora Technology Services Private Limited",
        "short_name": "Aspora",
        "addresses": [
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "Aswini Layout, Viveknagar",
                    "Bangalore 560047",
                    "India",
                ],
                "country": "India",
                "default": True,
            },
        ],
        "governing_law": {"playbook_option_id": "india", "label": "India"},
        # Entity-specific forum (the SOURCE OF TRUTH for the court written into the
        # generated "Governing law and jurisdiction" clause). India law, but a
        # Bengaluru/Karnataka venue -- a per-jurisdiction court map could NOT express
        # this, because Aspora Financial Services (also India) sits in Gandhinagar.
        "jurisdiction": "courts in Bengaluru, Karnataka",
        "incorporation_jurisdiction": "India",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
    {
        "id": "vance_money",
        "legal_name": "Vance Money Services LLC",
        "short_name": "Vance Money",
        "addresses": [
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "838 Walker Road",
                    "Dover, Delaware 19904",
                    "United States of America",
                ],
                "country": "United States of America",
                "default": True,
            },
        ],
        "governing_law": {"playbook_option_id": "delaware", "label": "Delaware"},
        # Entity-specific forum (source of truth for the rendered court).
        "jurisdiction": "courts in Delaware, USA",
        "incorporation_jurisdiction": "Delaware",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
    {
        "id": "real_transfer",
        "legal_name": "Real Transfer Limited",
        "short_name": "Real Transfer",
        # Two addresses. The London corporate office is the NDA default because it
        # is the one that maps cleanly to the playbook's England and Wales
        # governing-law position. The Belfast registered office sits in Northern
        # Ireland, which is a separate legal jurisdiction with NO matching
        # playbook position (see ENTITY_LAW_MAPPING_NOTES["real_transfer"]).
        "addresses": [
            {
                "id": "corporate",
                "label": "Corporate office",
                "lines": [
                    "3rd Floor",
                    "141-145 Curtain Road",
                    "London, EC2A 3BX",
                    "United Kingdom",
                ],
                "country": "United Kingdom",
                "default": True,
            },
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "Office 8, Merrion Business Centre",
                    "58 Howard Street",
                    "Belfast, Northern Ireland, BT1 6PJ",
                ],
                "country": "United Kingdom",
                "default": False,
            },
        ],
        "governing_law": {
            "playbook_option_id": "england_and_wales",
            "label": "England and Wales",
        },
        # Entity-specific forum (source of truth for the rendered court).
        "jurisdiction": "courts in England and Wales",
        # Legal confirmed England and Wales as the place of incorporation (not
        # Northern Ireland, despite the Belfast registered office) so it aligns
        # with the chosen governing law via the London corporate office.
        "incorporation_jurisdiction": "England and Wales",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
    {
        "id": "vance_techlabs",
        "legal_name": "Vance Techlabs Limited",
        "short_name": "Vance Techlabs",
        "addresses": [
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "Gate Avenue, DIFC",
                    "Dubai",
                    "United Arab Emirates",
                ],
                "country": "United Arab Emirates",
                "default": True,
            },
        ],
        # The entity sits in the DIFC free zone, a common-law jurisdiction with its
        # own DIFC Courts, distinct from UAE onshore/federal law. The playbook's
        # `difc` position is the right match. This would only be a gap if NDAs for
        # this entity were intended to run under UAE federal law, which the
        # playbook does not offer (see ENTITY_LAW_MAPPING_NOTES["vance_techlabs"]).
        "governing_law": {"playbook_option_id": "difc", "label": "DIFC"},
        # Entity-specific forum (source of truth for the rendered court). Phrased so
        # it reads grammatically in the clause: "the DIFC Courts shall have exclusive
        # jurisdiction ...".
        "jurisdiction": "the DIFC Courts",
        "incorporation_jurisdiction": "DIFC",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
    {
        "id": "nesse_technologies",
        "legal_name": "Nesse Technologies Inc",
        "short_name": "Nesse Technologies",
        "addresses": [
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "151 Yonge Street, 11th Floor",
                    "Toronto, Ontario M5C 2W7",
                    "Canada",
                ],
                "country": "Canada",
                "default": True,
            },
        ],
        "governing_law": {"playbook_option_id": "ontario_canada", "label": "Ontario, Canada"},
        # Entity-specific forum (source of truth for the rendered court).
        "jurisdiction": "the courts of Ontario, Canada",
        "incorporation_jurisdiction": "Ontario, Canada",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
    {
        "id": "vance_technologies",
        "legal_name": "Vance Technologies Limited",
        "short_name": "Vance Technologies",
        "addresses": [
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "Profile West, 950 Great West Road",
                    "Suite 2, First Floor",
                    "Brentford, TW8 9ES",
                    "United Kingdom",
                ],
                "country": "United Kingdom",
                "default": True,
            },
        ],
        "governing_law": {"playbook_option_id": "england_and_wales", "label": "England and Wales"},
        # Entity-specific forum (source of truth for the rendered court).
        "jurisdiction": "courts in England and Wales",
        "incorporation_jurisdiction": "England and Wales",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
    {
        "id": "aspora_financial_services",
        "legal_name": "Aspora Financial Services (IFSC) Private Limited",
        "short_name": "Aspora Financial Services",
        "addresses": [
            {
                "id": "registered",
                "label": "Registered office",
                "lines": [
                    "Cabin No. 03-05, 3rd floor",
                    "Flexone, Building 15C2",
                    "Gift City, Gandhi Nagar",
                    "Gandhi Nagar- 382050, Gujarat",
                ],
                "country": "India",
                "default": True,
            },
        ],
        "governing_law": {"playbook_option_id": "india", "label": "India"},
        # Entity-specific forum (source of truth for the rendered court). India law,
        # but a Gandhinagar/Gujarat venue -- DIFFERENT city from Aspora Technology
        # (Bengaluru) even though both share the playbook's `india` governing law.
        # This is exactly why the forum is entity-sourced, not per-jurisdiction.
        "jurisdiction": "courts in Gandhinagar, Gujarat",
        "incorporation_jurisdiction": "India",
        "signatory": {"name": "[Authorised Signatory]", "title": "[Title]"},
    },
]

# Human-readable notes for the two entities whose jurisdiction needed a judgement
# call between addresses/legal systems. Both calls were CONFIRMED by legal, so the
# defaults below are locked. Surfaced to reviewers; not consumed by generation
# logic.
ENTITY_LAW_MAPPING_NOTES: dict[str, str] = {
    "real_transfer": (
        "Real Transfer Limited has a registered office in Belfast (Northern "
        "Ireland) and a corporate office in London (England). Northern Ireland is "
        "a separate legal jurisdiction from England and Wales with no matching "
        "playbook position. CONFIRMED by legal: NDAs run under England and Wales "
        "law via the London corporate office (the default address); the Belfast "
        "registered office is retained for reference. No new playbook position is "
        "needed."
    ),
    "vance_techlabs": (
        "Vance Techlabs Limited is registered in the DIFC free zone, whose DIFC "
        "Courts are a distinct common-law jurisdiction from UAE federal/onshore "
        "law. CONFIRMED by legal: NDAs run under the playbook's `difc` position, "
        "not UAE federal law."
    ),
}


# ---------------------------------------------------------------------------
# Live registry (store-backed) accessors
# ---------------------------------------------------------------------------
#
# The accessors read the LIVE registry from the persistent store, seeded from
# DEFAULT_SIGNING_ENTITIES on first run. The store import is deferred to the call
# site to avoid an import cycle (entity_store imports checker.ROOT, and checker
# pulls in much of the review pipeline; entity_registry is imported very early by
# generation/counterparty modules).


def _live_entities() -> list[dict[str, Any]]:
    """Return the live registry bundles from the persistent store.

    Falls back to the in-repo defaults if the store module or disk is
    unavailable, so the registry is never empty and never crashes a reader.
    """

    try:
        from . import entity_store  # noqa: PLC0415 - deferred to avoid an import cycle

        return entity_store.load_entities(defaults=DEFAULT_SIGNING_ENTITIES)
    except Exception:  # noqa: BLE001 - a store/disk failure must never break a reader
        return [_copy_bundle(entity) for entity in DEFAULT_SIGNING_ENTITIES]


def list_entities() -> list[dict[str, Any]]:
    """Return all signing-entity bundles from the live registry."""

    return [_copy_bundle(entity) for entity in _live_entities()]


def get_entity(entity_id: str) -> dict[str, Any] | None:
    """Return the bundle for ``entity_id`` or ``None`` if it is not registered."""

    for entity in _live_entities():
        if entity.get("id") == entity_id:
            return _copy_bundle(entity)
    return None


def default_address(entity: Mapping[str, Any]) -> dict[str, Any] | None:
    """Return the address marked ``default`` for ``entity`` (or ``None``)."""

    for address in entity.get("addresses", []):
        if address.get("default"):
            return dict(address)
    return None


def actor_slug(entity: Mapping[str, Any]) -> str:
    """The actor identifier for this entity when it signs/generates an artifact.

    The artifact registry slugs whatever ``actor`` it is given into an artifact
    filename (``{seq}_{actor}_{role}_v{n}.ext``). The entity ``id`` is already a
    stable, short slug, so passing it as the actor yields clean, predictable
    names (e.g. ``aspora_technology`` -> ``...aspora-technology...``) rather than
    a truncated slug of the long legal name.
    """

    return str(entity.get("id") or "")


def _copy_bundle(entity: Mapping[str, Any]) -> dict[str, Any]:
    """Deep-ish copy so callers cannot mutate the module-level registry."""

    copied = dict(entity)
    copied["addresses"] = [dict(address) for address in entity.get("addresses", [])]
    copied["governing_law"] = dict(entity.get("governing_law", {}))
    copied["signatory"] = dict(entity.get("signatory", {}))
    return copied


# ---------------------------------------------------------------------------
# Validation
# ---------------------------------------------------------------------------


def _playbook_governing_law_option_ids(playbook: Mapping[str, Any]) -> set[str]:
    """Collect the governing_law approved_options ids from a playbook mapping."""

    return {option["id"] for option in _playbook_governing_law_options(playbook)}


def _playbook_governing_law_options(playbook: Mapping[str, Any]) -> list[dict[str, str]]:
    """The governing_law approved_options as ``[{"id", "label"}]`` from the playbook.

    This is the canonical source for the draft-intake override dropdown's law
    choices (id + display label), so the frontend does not have to derive them
    from the embedded entity mirror. Ids are taken verbatim; the label falls back
    to value then id when an option omits it. Order follows the playbook.
    """

    for clause in playbook.get("clauses", []):
        if clause.get("id") != "governing_law":
            continue
        rules = clause.get("rules") or {}
        options = rules.get("approved_options") or []
        result: list[dict[str, str]] = []
        for option in options:
            if not isinstance(option, Mapping):
                continue
            option_id = str(option.get("id") or "").strip()
            if not option_id:
                continue
            label = (
                str(option.get("label") or "").strip()
                or str(option.get("value") or "").strip()
                or option_id
            )
            result.append({"id": option_id, "label": label})
        return result
    return []


def _playbook_max_term_years(playbook: Mapping[str, Any]) -> int | None:
    """Read the term cap from the playbook's ``term_and_survival`` clause.

    The cap lives as a top-level ``max_term_years`` integer on the
    ``term_and_survival`` clause (the same field the generation clamp reads).
    Returns the live value, or ``None`` when the clause/field is missing or
    malformed so the caller can omit it and let the frontend fall back.
    """

    for clause in playbook.get("clauses", []):
        if clause.get("id") != "term_and_survival":
            continue
        value = clause.get("max_term_years")
        # bool is an int subclass; reject it explicitly so True/False can never
        # masquerade as a 1-year cap.
        if isinstance(value, bool) or not isinstance(value, int):
            return None
        if value < 1:
            return None
        return value
    return None


def validate_registry(entities: list[dict[str, Any]] | None = None) -> None:
    """Validate internal consistency of the bundles (no playbook needed).

    Checks that every bundle has the required fields and that each entity has
    exactly one default address. Raises ``ValueError`` on the first problem.

    When ``entities`` is supplied, that candidate list is validated (used by the
    authoring layer to vet a proposed save before it is persisted); otherwise the
    live registry is validated.
    """

    if entities is None:
        entities = _live_entities()

    if not isinstance(entities, list) or not entities:
        raise ValueError("The signing-entity registry must contain at least one entity.")

    seen_ids: set[str] = set()
    for entity in entities:
        if not isinstance(entity, dict):
            raise ValueError("Each signing entity must be an object.")
        entity_id = entity.get("id")
        if not entity_id:
            raise ValueError("Signing entity is missing an id.")
        if entity_id in seen_ids:
            raise ValueError(f"Duplicate signing entity id: {entity_id}.")
        seen_ids.add(entity_id)

        if not entity.get("legal_name"):
            raise ValueError(f"Entity {entity_id} is missing legal_name.")

        if not entity.get("incorporation_jurisdiction"):
            raise ValueError(
                f"Entity {entity_id} is missing incorporation_jurisdiction."
            )

        if not str(entity.get("jurisdiction") or "").strip():
            raise ValueError(
                f"Entity {entity_id} is missing a court/jurisdiction."
            )

        addresses = entity.get("addresses") or []
        if not addresses:
            raise ValueError(f"Entity {entity_id} has no addresses.")
        defaults = [a for a in addresses if isinstance(a, dict) and a.get("default")]
        if len(defaults) != 1:
            raise ValueError(
                f"Entity {entity_id} must have exactly one default address, "
                f"found {len(defaults)}."
            )
        for address in addresses:
            if not isinstance(address, dict):
                raise ValueError(f"Entity {entity_id} has a malformed address.")
            if not address.get("id"):
                raise ValueError(f"Entity {entity_id} has an address with no id.")
            if not address.get("lines"):
                raise ValueError(
                    f"Entity {entity_id} address {address.get('id')} has no lines."
                )

        law = entity.get("governing_law") or {}
        if not isinstance(law, dict) or not law.get("playbook_option_id"):
            raise ValueError(
                f"Entity {entity_id} is missing governing_law.playbook_option_id."
            )


def validate_registry_against_playbook(
    playbook: Mapping[str, Any],
    entities: list[dict[str, Any]] | None = None,
) -> None:
    """Validate the registry, then check the law mapping against ``playbook``.

    Every entity's ``governing_law.playbook_option_id`` must exist among the
    playbook's ``governing_law`` ``approved_options`` ids, AND each bundle's cached
    ``governing_law.label`` must equal that approved option's playbook label.
    Raises ``ValueError`` if the playbook has drifted away from a position a bundle
    relies on, or if a bundle's display label has drifted from the playbook's
    (the label is display-only -- the operative value already comes from the
    playbook via the id -- but asserting it here keeps the duplicated copy honest).

    When ``entities`` is supplied, that candidate list is checked against the
    playbook (used by the authoring layer to reject a save that points an entity
    at a non-approved law -- the ORPHAN GUARD); otherwise the live registry is
    checked.
    """

    if entities is None:
        entities = _live_entities()

    validate_registry(entities)

    options = _playbook_governing_law_options(playbook)
    if not options:
        raise ValueError(
            "Playbook has no governing_law approved_options to map entities to."
        )

    option_ids = {option["id"] for option in options}
    # Map each approved-option id to its playbook label so the cached
    # governing_law.label on every bundle can be asserted against the single
    # source of truth. The operative governing-law VALUE in generated docs is
    # already pulled from the playbook via the option id, so the cached label is
    # display-only -- but if it silently drifts from the playbook (an option
    # renamed without updating the bundle) the UI would show a stale jurisdiction
    # name. Asserting equality here fails loudly instead, closing that drift.
    option_label_by_id = {option["id"]: option["label"] for option in options}

    for entity in entities:
        option_id = entity["governing_law"]["playbook_option_id"]
        if option_id not in option_ids:
            raise ValueError(
                f"Entity {entity['id']} maps to governing-law position "
                f"'{option_id}', which is not an approved playbook option "
                f"(have: {sorted(option_ids)})."
            )
        cached_label = str(entity["governing_law"].get("label") or "")
        playbook_label = option_label_by_id[option_id]
        if cached_label != playbook_label:
            raise ValueError(
                f"Entity {entity['id']} caches governing-law label "
                f"'{cached_label}', which has drifted from the playbook label "
                f"'{playbook_label}' for option '{option_id}'. Update the bundle's "
                f"governing_law.label to match the playbook."
            )

    # Reconcile the two forum sources at jurisdiction-bucket granularity so the
    # review-side detector (playbook forum_jurisdiction) and the generator (registry
    # jurisdiction) cannot silently drift to different jurisdictions.
    validate_forum_reconciliation(playbook, entities)


def _forum_bucket(value: object) -> str:
    """Map a forum/law label to its jurisdiction bucket (or "").

    Reuses ``law_forum_check._normalize_to_bucket`` -- the same canonicaliser the
    review-side detector uses -- so the registry's ``jurisdiction`` strings and the
    playbook's ``forum_jurisdiction`` strings are bucketed by the SAME vocabulary
    and can be compared apples-to-apples. Defensive: an import/lookup failure yields
    "" so reconciliation degrades to "cannot bucket" rather than crashing a caller.
    """

    try:
        from . import law_forum_check  # noqa: PLC0415

        return str(law_forum_check._normalize_to_bucket(value) or "")
    except Exception:  # noqa: BLE001 - bucketing is best-effort.
        return ""


def validate_forum_reconciliation(
    playbook: Mapping[str, Any],
    entities: list[dict[str, Any]] | None = None,
) -> None:
    """Reconcile each entity's court against its OWN governing law's jurisdiction.

    The per-entity ``jurisdiction`` (the city-level court that GENERATION writes into
    a signed NDA, e.g. "courts in Bengaluru") must sit in the SAME jurisdiction bucket
    (country / legal system) as the governing law that entity defaults to. Otherwise
    the generator would write a court in a different jurisdiction than the law it
    pairs with -- and the review side, which derives the expected forum from the law,
    would flag the mismatch.

    The expected jurisdiction bucket for an option is derived from the LAW's own
    id/label via ``law_forum_check.expected_forum_bucket`` (each approved governing-law
    option's proper forum sits in its own jurisdiction, e.g. india law -> courts in
    India). There is no longer a per-option ``forum_jurisdiction`` field to read --
    the court is now edited per entity, and the law is its own forum source.

    Raises ``ValueError`` on the first mismatch so the drift-guard / publish gate can
    surface a clear error.

    When ``entities`` is supplied, that CANDIDATE list is reconciled against the
    playbook (the authoring layer passes the entities being saved, so a mismatched
    law/court pairing is caught BEFORE persistence); otherwise the live registry is
    reconciled.
    """

    if entities is None:
        entities = _live_entities()

    validate_registry(entities)

    options = _playbook_governing_law_options(playbook)
    if not options:
        raise ValueError(
            "Playbook has no governing_law approved_options to reconcile forums against."
        )

    try:
        from . import law_forum_check  # noqa: PLC0415
    except Exception as exc:  # noqa: BLE001 - missing helper is a hard error here.
        raise ValueError(
            "law_forum_check is unavailable; cannot derive the expected forum bucket "
            f"from each governing-law option ({exc})."
        ) from exc

    option_ids = {option["id"] for option in options}

    # option id -> the jurisdiction bucket implied by the LAW itself (its own
    # id/label), via the same primitive the review-side detector uses.
    option_bucket_by_id: dict[str, str] = {}
    for option_id in option_ids:
        bucket = str(law_forum_check.expected_forum_bucket(playbook, option_id) or "")
        if bucket:
            option_bucket_by_id[option_id] = bucket

    for entity in entities:
        option_id = entity["governing_law"]["playbook_option_id"]
        option_bucket = option_bucket_by_id.get(option_id)
        if option_bucket is None:
            # Either the option id is unknown (owned by
            # validate_registry_against_playbook) or its law doesn't resolve to a
            # known bucket -- in both cases there is nothing to reconcile against
            # here, so skip silently rather than block the save.
            continue
        entity_forum = str(entity.get("jurisdiction") or "").strip()
        if not entity_forum:
            raise ValueError(
                f"Entity {entity['id']} is missing a 'jurisdiction' (the court written "
                "into a generated NDA)."
            )
        entity_bucket = _forum_bucket(entity_forum)
        if not entity_bucket:
            raise ValueError(
                f"Entity {entity['id']} jurisdiction '{entity_forum}' does not resolve "
                "to any known jurisdiction forum bucket."
            )
        if entity_bucket != option_bucket:
            raise ValueError(
                f"Forum drift: entity {entity['id']} jurisdiction '{entity_forum}' "
                f"resolves to bucket '{entity_bucket}', but its governing law "
                f"'{option_id}' implies jurisdiction bucket '{option_bucket}'. The "
                "generator would write a court in a different jurisdiction than this "
                "entity's governing law."
            )


def entity_law_mapping(playbook: Mapping[str, Any] | None = None) -> list[dict[str, Any]]:
    """Return the entity -> governing-law mapping for reporting/UI.

    When ``playbook`` is supplied, each row carries ``matches_playbook`` so a
    caller can surface drift. ``flag`` carries any human note from
    :data:`ENTITY_LAW_MAPPING_NOTES`.
    """

    option_ids: set[str] = (
        _playbook_governing_law_option_ids(playbook) if playbook is not None else set()
    )
    rows: list[dict[str, Any]] = []
    for entity in _live_entities():
        option_id = entity["governing_law"]["playbook_option_id"]
        row: dict[str, Any] = {
            "entity_id": entity["id"],
            "legal_name": entity["legal_name"],
            "playbook_option_id": option_id,
            "law_label": entity["governing_law"].get("label"),
            "flag": ENTITY_LAW_MAPPING_NOTES.get(entity["id"]),
        }
        if playbook is not None:
            row["matches_playbook"] = option_id in option_ids
        rows.append(row)
    return rows


def signing_entities_payload(playbook: Mapping[str, Any] | None = None) -> dict[str, Any]:
    """Assemble the API payload the draft-intake UI consumes.

    Returns the entity bundles, the entity -> governing-law mapping (with
    ``matches_playbook`` drift flags when ``playbook`` is supplied), the live
    set of playbook governing-law option ids, and the ``governing_law_options``
    ({id,label}) list — both sourced from the playbook's governing_law
    approved_options so the UI's override dropdown can be playbook-driven rather
    than derived from the embedded entity mirror.

    When the playbook is supplied and carries a usable term cap, the payload also
    exposes ``playbook_meta.max_term_years`` sourced live from the playbook's
    ``term_and_survival`` clause, so the Generator's term cap is playbook-driven
    rather than a hardcoded duplicate. The field is omitted (not invented) when
    the cap is missing or malformed; the frontend keeps its own fallback.
    """

    option_ids = (
        sorted(_playbook_governing_law_option_ids(playbook))
        if playbook is not None
        else []
    )
    payload: dict[str, Any] = {
        "entities": list_entities(),
        "law_mapping": entity_law_mapping(playbook),
        "playbook_option_ids": option_ids,
        # The governing-law dropdown choices ({id,label}) sourced live from the
        # playbook's governing_law approved_options, so the draft-intake override
        # dropdown is playbook-driven rather than derived from the embedded entity
        # mirror. Empty when no playbook is supplied; the frontend then falls back
        # to its entity-derived options.
        "governing_law_options": (
            _playbook_governing_law_options(playbook) if playbook is not None else []
        ),
    }
    max_term_years = (
        _playbook_max_term_years(playbook) if playbook is not None else None
    )
    if max_term_years is not None:
        payload["playbook_meta"] = {"max_term_years": max_term_years}
    return payload


# Backward-compatibility alias: the hardcoded list used to be the live registry.
# It is now the seed default. A few call sites / tests may still reference the old
# name; keep it pointing at the defaults (which is what it always contained).
SIGNING_ENTITIES = DEFAULT_SIGNING_ENTITIES

__all__ = [
    "DEFAULT_SIGNING_ENTITIES",
    "SIGNING_ENTITIES",
    "ENTITY_LAW_MAPPING_NOTES",
    "list_entities",
    "get_entity",
    "default_address",
    "actor_slug",
    "validate_registry",
    "validate_registry_against_playbook",
    "validate_forum_reconciliation",
    "entity_law_mapping",
    "signing_entities_payload",
    "_playbook_max_term_years",
]
