const ANNOTATION_TYPES = new Set(["comment", "highlight", "strikethrough"]);
const DRAWING_TOOLS = new Set(["comment", "highlight", "strikethrough"]);
const DEFAULT_TOOL = "cursor";
const MIN_DRAG_FRACTION = 0.01;

export function clamp01(value) {
  const number = Number(value);
  if (!Number.isFinite(number)) return 0;
  if (number < 0) return 0;
  if (number > 1) return 1;
  return number;
}

export function positiveInt(value) {
  const number = Number(value);
  return Number.isFinite(number) && number > 0 ? Math.floor(number) : null;
}

export function normalizeRect(rect, type = "") {
  const source = rect && typeof rect === "object" ? rect : {};
  const normalized = {
    h: clamp01(source.h),
    w: clamp01(source.w),
    x: clamp01(source.x),
    y: clamp01(source.y),
  };
  if (type === "comment") {
    normalized.w = 0;
    normalized.h = 0;
  }
  return normalized;
}

export function normalizeAnnotation(raw, { fallbackId = "" } = {}) {
  if (!raw || typeof raw !== "object") return null;
  const type = String(raw.type || "").trim().toLowerCase();
  if (!ANNOTATION_TYPES.has(type)) return null;
  const page = positiveInt(raw.page);
  if (!page) return null;
  const annotation = {
    id: raw.id == null ? String(fallbackId || "") : String(raw.id),
    page,
    rect: normalizeRect(raw.rect, type),
    type,
  };
  if (!annotation.id) return null;
  if (raw.text != null) annotation.text = String(raw.text);
  if (raw.color != null) annotation.color = String(raw.color);
  if (raw.author != null) annotation.author = String(raw.author);
  if (raw.created_at != null) annotation.created_at = String(raw.created_at);
  return annotation;
}

export function normalizeAnnotations(list, { fallbackIdPrefix = "local" } = {}) {
  if (!Array.isArray(list)) return [];
  return list
    .map((annotation, index) => normalizeAnnotation(annotation, { fallbackId: `${fallbackIdPrefix}-${index + 1}` }))
    .filter(Boolean);
}

export function pointFromClientRect(clientPoint, bounds) {
  const rect = bounds && typeof bounds === "object" ? bounds : {};
  return {
    x: rect.width ? clamp01((Number(clientPoint?.clientX) - Number(rect.left || 0)) / Number(rect.width)) : 0,
    y: rect.height ? clamp01((Number(clientPoint?.clientY) - Number(rect.top || 0)) / Number(rect.height)) : 0,
  };
}

export function rectFromPoints(a, b) {
  const start = a && typeof a === "object" ? a : {};
  const end = b && typeof b === "object" ? b : {};
  const x = Math.min(clamp01(start.x), clamp01(end.x));
  const y = Math.min(clamp01(start.y), clamp01(end.y));
  const w = Math.abs(clamp01(start.x) - clamp01(end.x));
  const h = Math.abs(clamp01(start.y) - clamp01(end.y));
  return { h: clamp01(h), w: clamp01(w), x: clamp01(x), y: clamp01(y) };
}

export function dragHasDrawableArea(rect, minFraction = MIN_DRAG_FRACTION) {
  return Boolean(rect && Number(rect.w) >= minFraction && Number(rect.h) >= minFraction);
}

export function overlayStyle(rect, bounds) {
  const box = normalizeRect(rect);
  const width = Number(bounds?.width || 0);
  const height = Number(bounds?.height || 0);
  return {
    height: `${box.h * height}px`,
    left: `${box.x * width}px`,
    top: `${box.y * height}px`,
    width: `${box.w * width}px`,
  };
}

export function commentPositionStyle(point, bounds) {
  const normalized = normalizeRect({ ...point, h: 0, w: 0 }, "comment");
  const width = Number(bounds?.width || 0);
  const height = Number(bounds?.height || 0);
  return {
    left: `${normalized.x * width}px`,
    top: `${normalized.y * height}px`,
  };
}

export function createInitialState(overrides = {}) {
  return {
    activeTool: DEFAULT_TOOL,
    annotations: [],
    loadedMatterId: null,
    loadSequence: 0,
    mounted: false,
    openPopoverId: null,
    savingIds: [],
    deletingIds: [],
    ...overrides,
  };
}

export function toolIsDrawing(toolId) {
  return DRAWING_TOOLS.has(String(toolId || ""));
}

export function setActiveTool(state, toolId) {
  const nextTool = toolId === DEFAULT_TOOL || DRAWING_TOOLS.has(String(toolId || "")) ? String(toolId) : state.activeTool;
  return {
    ...state,
    activeTool: nextTool || DEFAULT_TOOL,
    openPopoverId: null,
  };
}

export function startLoad(state, matterId) {
  return {
    ...state,
    annotations: state.loadedMatterId === matterId ? state.annotations : [],
    loadSequence: Number(state.loadSequence || 0) + 1,
    loadedMatterId: state.loadedMatterId === matterId ? state.loadedMatterId : null,
  };
}

export function completeLoad(state, matterId, sequence, annotations) {
  if (Number(sequence) !== Number(state.loadSequence || 0)) return state;
  return {
    ...state,
    annotations: normalizeAnnotations(annotations),
    loadedMatterId: String(matterId || ""),
  };
}

export function appendAnnotation(state, annotation) {
  const normalized = normalizeAnnotation(annotation);
  if (!normalized) return state;
  return {
    ...state,
    annotations: [...state.annotations, normalized],
  };
}

export function removeAnnotation(state, annotationId) {
  const id = String(annotationId);
  return {
    ...state,
    annotations: state.annotations.filter((annotation) => String(annotation.id) !== id),
    openPopoverId: String(state.openPopoverId) === id ? null : state.openPopoverId,
  };
}

export function togglePopover(state, annotationId) {
  const id = annotationId == null ? null : String(annotationId);
  return {
    ...state,
    openPopoverId: state.openPopoverId === id ? null : id,
  };
}

export function annotationPayload({ page, type, rect, text, color } = {}) {
  const payload = {
    page: positiveInt(page),
    rect: normalizeRect(rect, type),
    type: String(type || "").trim().toLowerCase(),
  };
  if (!payload.page || !ANNOTATION_TYPES.has(payload.type)) return null;
  if (text != null) payload.text = String(text);
  if (color != null) payload.color = String(color);
  return payload;
}

export function markedUpFilename({ matterId = "", selectedMatter = {} } = {}) {
  const base = String(selectedMatter.source_filename || selectedMatter.attachment_filename || "").trim();
  const stem = base.replace(/\.[^.]*$/, "");
  const safe = Array.from(stem || `matter-${matterId}`)
    .map((character) => (/[a-z0-9_-]/i.test(character) ? character : "-"))
    .join("")
    .replace(/^[-_]+/g, "")
    .replace(/[-_]+$/g, "");
  return `${safe || "nda"}-marked-up.pdf`;
}

export const PdfMarkupWorkstation = {
  annotationPayload,
  appendAnnotation,
  clamp01,
  commentPositionStyle,
  completeLoad,
  createInitialState,
  dragHasDrawableArea,
  markedUpFilename,
  normalizeAnnotation,
  normalizeAnnotations,
  normalizeRect,
  overlayStyle,
  pointFromClientRect,
  positiveInt,
  rectFromPoints,
  removeAnnotation,
  setActiveTool,
  startLoad,
  togglePopover,
  toolIsDrawing,
};
