// Pure, testable helpers for the OUTBOUND-DRAFT intake flow.
//
// This is the heart of the entity picker's "optionality": picking one of our
// signing entities pre-fills its legal name, address, and governing law as a
// single coupled bundle, so a user can never accidentally pair (say) the UK
// entity with Delaware law. The governing law stays independently overridable
// (an escape hatch) but defaults to the picked entity's law, and an entity with
// two addresses lets the user choose which one signs.
//
// The browser controller (static/js/draft-intake.js) mirrors this logic; these
// exports are the single source the frontend tests exercise.
//
// ── Entity registry seam ──────────────────────────────────────────────────
// SIGNING_ENTITIES below mirrors the entity-model registry
// (nda_automation/entity_registry.py) field-for-field: the same entity ids,
// legal/short names, address shape ({id,label,lines,country,default}), and the
// governing_law bundle keyed on `playbook_option_id` (the join key into the
// playbook's governing_law approved_options). When entity-model ships a
// /api/signing-entities endpoint, the controller can fetch that JSON and feed it
// straight through `createDraftIntake({ entities })` — every helper here already
// reads through the registry argument and through the same field names, so it is
// a drop-in with no logic change.
//
// The governing-law option id is read via lawOptionId() which prefers
// `playbook_option_id` (entity-model's contract) and falls back to `id`, so the
// helpers work against either the embedded copy or a future shape.

const EMAIL_PATTERN = /^[^\s@]+@[^\s@]+\.[^\s@]+$/;

// The playbook makes mutuality a REQUIRED clause and explicitly fails one-way
// confidentiality (mutuality.rules.fail_conditions.one_way_party_roles), so a
// one-way NDA would fail our own review. The Generator therefore offers mutual
// only; the backend likewise rejects any non-mutual nda_type as a backstop.
export const NDA_TYPES = [
  { id: "mutual", label: "Mutual (two-way)" },
];

export const DEFAULT_NDA_TYPE = "mutual";

// The playbook caps the NDA term at max_term_years; generation clamps a longer
// requested term down to this (nda_generation._resolve_term_years). The live
// value rides the /api/signing-entities feed as `playbook_meta.max_term_years`
// (a backend teammate adds it in parallel); this constant is the fallback the
// embedded mirror uses so the preview's "capped to N years" note stays correct
// even before the live value is wired. Keep in sync with the playbook's
// term_and_survival.max_term_years.
export const DEFAULT_MAX_TERM_YEARS = 5;

// NOTE: the generated NDA's "GOVERNING LAW AND JURISDICTION" clause is LAW-ONLY —
// it states the governing law and names no forum/courts (a single governing law
// may be litigated in more than one court, and generation deliberately omits the
// courts sentence to match how review reads the clause; see
// nda_generation._fill_variable_slots / the law-only template). The preview must
// therefore NOT show a forum/courts sentence either, so there is no
// FORUM_BY_OPTION_ID mirror or forum-resolution helper here. The backend still
// records a provenance-only `forum` on the manifest, but it is never written into
// the document, so it is not previewed.

// Our seven signing entities, mirroring nda_automation/entity_registry.py. Each
// bundle travels together: legal_name + governing_law + addresses are a unit
// keyed by `id`. `addresses` is always non-empty with exactly one `default:true`
// address; Real Transfer carries two (London corporate office is the NDA default
// — it maps to the England & Wales playbook position; the Belfast registered
// office is the alternate).
export const SIGNING_ENTITIES = [
  {
    id: "aspora_technology",
    short_name: "Aspora",
    legal_name: "Aspora Technology Services Private Limited",
    governing_law: { playbook_option_id: "india", label: "India" },
    addresses: [
      {
        id: "registered",
        label: "Registered office",
        lines: ["Aswini Layout, Viveknagar", "Bangalore 560047", "India"],
        country: "India",
        default: true,
      },
    ],
  },
  {
    id: "vance_money",
    short_name: "Vance Money",
    legal_name: "Vance Money Services LLC",
    governing_law: { playbook_option_id: "delaware", label: "Delaware" },
    addresses: [
      {
        id: "registered",
        label: "Registered office",
        lines: ["838 Walker Road", "Dover, Delaware 19904", "United States of America"],
        country: "United States of America",
        default: true,
      },
    ],
  },
  {
    // Real Transfer — the entity with two addresses. London corporate office is
    // the default (maps to England & Wales); Belfast registered office is the
    // alternate (Northern Ireland has no matching playbook position).
    id: "real_transfer",
    short_name: "Real Transfer",
    legal_name: "Real Transfer Limited",
    governing_law: { playbook_option_id: "england_and_wales", label: "England and Wales" },
    addresses: [
      {
        id: "corporate",
        label: "Corporate office",
        lines: ["3rd Floor", "141-145 Curtain Road", "London, EC2A 3BX", "United Kingdom"],
        country: "United Kingdom",
        default: true,
      },
      {
        id: "registered",
        label: "Registered office",
        lines: ["Office 8, Merrion Business Centre", "58 Howard Street", "Belfast, Northern Ireland, BT1 6PJ"],
        country: "United Kingdom",
        default: false,
      },
    ],
  },
  {
    id: "vance_techlabs",
    short_name: "Vance Techlabs",
    legal_name: "Vance Techlabs Limited",
    governing_law: { playbook_option_id: "difc", label: "DIFC" },
    addresses: [
      {
        id: "registered",
        label: "Registered office",
        lines: ["Gate Avenue, DIFC", "Dubai", "United Arab Emirates"],
        country: "United Arab Emirates",
        default: true,
      },
    ],
  },
  {
    id: "nesse_technologies",
    short_name: "Nesse Technologies",
    legal_name: "Nesse Technologies Inc",
    governing_law: { playbook_option_id: "ontario_canada", label: "Ontario, Canada" },
    addresses: [
      {
        id: "registered",
        label: "Registered office",
        lines: ["151 Yonge Street, 11th Floor", "Toronto, Ontario M5C 2W7", "Canada"],
        country: "Canada",
        default: true,
      },
    ],
  },
  {
    id: "vance_technologies",
    short_name: "Vance Technologies",
    legal_name: "Vance Technologies Limited",
    governing_law: { playbook_option_id: "england_and_wales", label: "England and Wales" },
    addresses: [
      {
        id: "registered",
        label: "Registered office",
        lines: ["Profile West, 950 Great West Road", "Suite 2, First Floor", "Brentford, TW8 9ES", "United Kingdom"],
        country: "United Kingdom",
        default: true,
      },
    ],
  },
  {
    id: "aspora_financial_services",
    short_name: "Aspora Financial Services",
    legal_name: "Aspora Financial Services (IFSC) Private Limited",
    governing_law: { playbook_option_id: "india", label: "India" },
    addresses: [
      {
        id: "registered",
        label: "Registered office",
        lines: ["Cabin No. 03-05, 3rd floor", "Flexone, Building 15C2", "Gift City, Gandhi Nagar", "Gandhi Nagar- 382050, Gujarat"],
        country: "India",
        default: true,
      },
    ],
  },
];

// The governing-law option id for a law bundle. Prefers entity-model's
// `playbook_option_id` (the join key into the playbook) and falls back to `id`.
export function lawOptionId(law) {
  return law?.playbook_option_id || law?.id || null;
}

// The dropdown label for an entity: its short_name if present, else legal_name.
export function entityLabel(entity) {
  return entity?.short_name || entity?.legal_name || entity?.id || "";
}

// The governing-law choices offered in the independent-override dropdown.
//
// SINGLE SOURCE OF TRUTH: when the controller has the live playbook governing-law
// options (the /api/signing-entities feed's `governing_law_options`, sourced from
// the playbook's governing_law approved_options), it passes them as `lawOptions`
// and the dropdown is playbook-driven. Absent that (offline / endpoint not
// deployed), we fall back to deriving the distinct laws across the embedded
// entity mirror — every entity law IS a playbook option id, so the fallback set
// still matches the playbook's approved positions.
export function governingLawOptions(entities = SIGNING_ENTITIES, lawOptions = null) {
  if (Array.isArray(lawOptions) && lawOptions.length) {
    const seen = new Map();
    for (const option of lawOptions) {
      const id = option?.id;
      if (id && !seen.has(id)) {
        seen.set(id, { id, label: option.label || id });
      }
    }
    if (seen.size) return Array.from(seen.values());
  }
  const seen = new Map();
  for (const entity of entities || []) {
    const law = entity?.governing_law;
    const id = lawOptionId(law);
    if (id && !seen.has(id)) {
      seen.set(id, { id, label: law.label || id });
    }
  }
  return Array.from(seen.values());
}

export function findEntity(entityId, entities = SIGNING_ENTITIES) {
  return (entities || []).find((entity) => entity?.id === entityId) || null;
}

// The address an entity defaults to: the one flagged default, else the first.
export function defaultAddressFor(entity) {
  const addresses = entity?.addresses || [];
  return addresses.find((address) => address?.default) || addresses[0] || null;
}

export function findAddress(entity, addressId) {
  return (entity?.addresses || []).find((address) => address?.id === addressId) || null;
}

export function hasMultipleAddresses(entity) {
  return (entity?.addresses || []).length > 1;
}

// Renders an address's lines as a single display string. Used by the form's
// read-only address preview and by the captured payload.
export function formatAddressLines(address) {
  return (address?.lines || []).map((line) => String(line || "").trim()).filter(Boolean).join(", ");
}

// The empty intake state. governingLawId is null until an entity is picked (it
// then defaults to that entity's law) or the user overrides it directly.
export function createInitialIntake() {
  return {
    counterpartyName: "",
    counterpartyEmail: "",
    projectPurpose: "",
    term: "",
    ndaType: DEFAULT_NDA_TYPE,
    entityId: null,
    addressId: null,
    governingLawId: null,
    // Tracks whether the user has overridden the law away from the entity's
    // default, so re-picking an entity can restore the coupled law unless the
    // user has deliberately taken the escape hatch.
    governingLawOverridden: false,
  };
}

// Picks a signing entity and pre-fills the coupled bundle: address defaults to
// the entity's default address, and governing law defaults to the entity's law
// UNLESS the user has already taken the override escape hatch — then their
// chosen law is preserved (the whole point of an independent override).
export function applyEntitySelection(intake, entityId, entities = SIGNING_ENTITIES) {
  const entity = findEntity(entityId, entities);
  if (!entity) {
    return { ...intake, entityId: null, addressId: null };
  }
  const next = { ...intake, entityId: entity.id };
  const defaultAddress = defaultAddressFor(entity);
  next.addressId = defaultAddress ? defaultAddress.id : null;
  if (!intake.governingLawOverridden) {
    next.governingLawId = lawOptionId(entity.governing_law);
  }
  return next;
}

// The escape hatch: override the governing law independently of the entity.
// Marks the override so a later entity re-pick won't silently stomp the choice.
export function setGoverningLawOverride(intake, lawId) {
  return { ...intake, governingLawId: lawId || null, governingLawOverridden: true };
}

// Drops the override and re-couples the law to the picked entity's law.
export function clearGoverningLawOverride(intake, entities = SIGNING_ENTITIES) {
  const entity = findEntity(intake.entityId, entities);
  return {
    ...intake,
    governingLawOverridden: false,
    governingLawId: lawOptionId(entity?.governing_law),
  };
}

// Picks which address signs (only meaningful for the two-address entity).
// Ignores ids that don't belong to the picked entity.
export function selectAddress(intake, addressId, entities = SIGNING_ENTITIES) {
  const entity = findEntity(intake.entityId, entities);
  if (!findAddress(entity, addressId)) {
    return intake;
  }
  return { ...intake, addressId };
}

// The governing law currently in effect for an intake: the explicit choice if
// any, else the picked entity's law. Returns the {id,label} object so callers
// (and the payload) carry a coupled, labelled value — never a bare id that
// could be mis-paired with a label elsewhere.
export function effectiveGoverningLaw(intake, entities = SIGNING_ENTITIES, lawOptions = null) {
  const entity = findEntity(intake.entityId, entities);
  const lawId = intake.governingLawId || lawOptionId(entity?.governing_law);
  if (!lawId) return null;
  const fromOptions = governingLawOptions(entities, lawOptions).find((law) => law.id === lawId);
  if (fromOptions) return fromOptions;
  // An overridden law id should still resolve to its entity label when it is the
  // entity's own law; otherwise surface the id as its own label.
  if (lawOptionId(entity?.governing_law) === lawId) {
    return { id: lawId, label: entity.governing_law.label || lawId };
  }
  return { id: lawId, label: lawId };
}

export function selectedEntity(intake, entities = SIGNING_ENTITIES) {
  return findEntity(intake.entityId, entities);
}

export function selectedAddress(intake, entities = SIGNING_ENTITIES) {
  const entity = findEntity(intake.entityId, entities);
  if (!entity) return null;
  return findAddress(entity, intake.addressId) || defaultAddressFor(entity);
}

export function isValidCounterpartyEmail(value) {
  const trimmed = String(value || "").trim();
  return trimmed === "" || EMAIL_PATTERN.test(trimmed);
}

// Returns { ok, error } describing whether the intake can proceed to Generate.
// The entity (and therefore a coupled law + address) plus a counterparty name
// are the minimum to draft an outbound NDA; the optional email, if present,
// must be well formed.
export function validateDraftIntake(intake = {}, entities = SIGNING_ENTITIES, lawOptions = null) {
  if (!String(intake.counterpartyName || "").trim()) {
    return { ok: false, error: "Enter the counterparty name." };
  }
  if (!intake.entityId || !findEntity(intake.entityId, entities)) {
    return { ok: false, error: "Pick the Aspora signing entity." };
  }
  if (!isValidCounterpartyEmail(intake.counterpartyEmail)) {
    return { ok: false, error: "Enter a valid counterparty email, or leave it blank." };
  }
  if (!effectiveGoverningLaw(intake, entities, lawOptions)) {
    return { ok: false, error: "Pick a governing law." };
  }
  return { ok: true, error: "" };
}

// Builds the captured payload the (stubbed) generation step will consume. The
// signing-entity block is emitted as a coupled unit — legal_name, address, and
// governing_law together — so downstream generation receives the bundle, not
// three loose fields that could be recombined incorrectly.
export function buildDraftPayload(intake = {}, entities = SIGNING_ENTITIES, lawOptions = null) {
  const entity = findEntity(intake.entityId, entities);
  const address = selectedAddress(intake, entities);
  const law = effectiveGoverningLaw(intake, entities, lawOptions);
  return {
    counterparty: {
      name: String(intake.counterpartyName || "").trim(),
      email: String(intake.counterpartyEmail || "").trim() || null,
    },
    project_purpose: String(intake.projectPurpose || "").trim(),
    term: String(intake.term || "").trim(),
    nda_type: intake.ndaType || DEFAULT_NDA_TYPE,
    // First-party recital + identity fields the preview already shows and the
    // generator reads (mapped to the template's [BUSINESS DESCRIPTION] /
    // first-party jurisdiction + registered-office slots). These were previously
    // dropped from the payload even though the preview rendered them, so a
    // generated NDA silently lost the recital business line and the counterparty's
    // incorporation/office. Key names are fixed by the backend contract — do not
    // rename: business_description, counterparty_jurisdiction,
    // counterparty_registered_office.
    business_description: String(intake.businessDescription || "").trim(),
    counterparty_jurisdiction: String(intake.counterpartyIncorporation || "").trim(),
    counterparty_registered_office: String(intake.counterpartyAddress || "").trim(),
    signing_entity: entity
      ? {
          id: entity.id,
          legal_name: entity.legal_name,
          address: address
            ? { id: address.id, label: address.label, lines: [...(address.lines || [])] }
            : null,
          // governing_law carries the playbook_option_id join key (entity-model's
          // contract) so downstream generation pulls the matching approved clause.
          governing_law: law
            ? { playbook_option_id: law.id, label: law.label }
            : null,
          // True when the law no longer matches the entity's own law — a signal
          // for generation/review that the coupling was deliberately broken.
          governing_law_overridden: Boolean(
            law && entity.governing_law && law.id !== lawOptionId(entity.governing_law),
          ),
        }
      : null,
  };
}

// Factory that binds a registry instance, returning the same helper surface
// pre-bound to it. Lets the controller (and the eventual real registry) inject
// entities once instead of threading them through every call.
//
// `lawOptions` (the playbook's governing_law approved_options as [{id,label}],
// from the /api/signing-entities feed) makes the override dropdown playbook-driven
// rather than entity-derived. When omitted, the helpers fall back to deriving the
// laws from the embedded entity mirror — so the picker stays fully functional
// before the live feed loads or when offline.
export function createDraftIntake({ entities = SIGNING_ENTITIES, lawOptions = null } = {}) {
  return {
    entities,
    lawOptions,
    ndaTypes: NDA_TYPES,
    governingLawOptions: () => governingLawOptions(entities, lawOptions),
    createInitialIntake,
    findEntity: (id) => findEntity(id, entities),
    entityLabel,
    lawOptionId,
    selectedEntity: (intake) => selectedEntity(intake, entities),
    selectedAddress: (intake) => selectedAddress(intake, entities),
    defaultAddressFor,
    hasMultipleAddresses,
    formatAddressLines,
    applyEntitySelection: (intake, id) => applyEntitySelection(intake, id, entities),
    setGoverningLawOverride,
    clearGoverningLawOverride: (intake) => clearGoverningLawOverride(intake, entities),
    selectAddress: (intake, id) => selectAddress(intake, id, entities),
    effectiveGoverningLaw: (intake) => effectiveGoverningLaw(intake, entities, lawOptions),
    validateDraftIntake: (intake) => validateDraftIntake(intake, entities, lawOptions),
    buildDraftPayload: (intake) => buildDraftPayload(intake, entities, lawOptions),
  };
}
