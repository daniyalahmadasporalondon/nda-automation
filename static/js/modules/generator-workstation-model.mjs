export const GENERATOR_MODE_DRAFT = "draft";
export const GENERATOR_MODE_GENERATED = "generated";
export const GENERATOR_HISTORY_LIMIT = 60;

export function cloneParagraph(paragraph = {}) {
  const copy = { ...paragraph };
  if (Array.isArray(paragraph.runs)) copy.runs = paragraph.runs.map((run) => ({ ...run }));
  return copy;
}

export function cloneParagraphs(paragraphs) {
  return Array.isArray(paragraphs) ? paragraphs.map(cloneParagraph) : [];
}

export function generatorParagraphs(workstation) {
  return Array.isArray(workstation?.generatorParagraphs) ? workstation.generatorParagraphs : [];
}

export function generatorHistory(workstation) {
  return Array.isArray(workstation?.generatorHistory) ? workstation.generatorHistory : [];
}

export function generatorParagraphById(workstation, paragraphId) {
  const id = String(paragraphId ?? "");
  if (!id) return null;
  return generatorParagraphs(workstation).find((paragraph) => String(paragraph.id) === id) || null;
}

export function activeGeneratorParagraph(workstation) {
  return generatorParagraphById(workstation, workstation?.generatorActiveParagraphId);
}

export function draftGeneratorState(paragraphs) {
  return {
    generatorActiveParagraphId: null,
    generatorHistory: [],
    generatorMatterId: null,
    generatorMode: GENERATOR_MODE_DRAFT,
    generatorParagraphs: cloneParagraphs(paragraphs),
  };
}

export function generatedGeneratorState({ matterId, paragraphs } = {}) {
  const nextParagraphs = cloneParagraphs(paragraphs);
  return {
    generatorActiveParagraphId: null,
    generatorDraftTouched: false,
    generatorHistory: [],
    generatorMatterId: matterId || null,
    generatorMode: GENERATOR_MODE_GENERATED,
    generatorOriginalParagraphs: cloneParagraphs(nextParagraphs),
    generatorParagraphs: nextParagraphs,
  };
}

export function clearGeneratorState() {
  return {
    generatorActiveParagraphId: null,
    generatorDraftTouched: false,
    generatorHistory: [],
    generatorMatterId: null,
    generatorMode: GENERATOR_MODE_DRAFT,
    generatorParagraphs: [],
  };
}

export function generatorEditSnapshot(workstation) {
  return {
    dirty: Boolean(workstation?.generatorDraftTouched),
    matterId: workstation?.generatorMatterId || null,
    mode: workstation?.generatorMode || GENERATOR_MODE_DRAFT,
    paragraphs: generatorParagraphs(workstation).map((paragraph) => {
      const snapshot = {
        id: paragraph.id,
        text: String(paragraph.text || ""),
      };
      if (Array.isArray(paragraph.runs)) snapshot.runs = paragraph.runs;
      if (paragraph.alignment !== undefined) snapshot.alignment = paragraph.alignment;
      if (paragraph.font !== undefined) snapshot.font = paragraph.font;
      if (paragraph.fontSize !== undefined) snapshot.fontSize = paragraph.fontSize;
      if (paragraph.source_index !== undefined) snapshot.source_index = paragraph.source_index;
      if (paragraph.source_part !== undefined) snapshot.source_part = paragraph.source_part;
      return snapshot;
    }),
  };
}

export function generatorExportReady(workstation, redlines = []) {
  return workstation?.generatorMode === GENERATOR_MODE_GENERATED
    && Boolean(workstation?.generatorMatterId)
    && Array.isArray(redlines)
    && redlines.length > 0;
}

export function snapshotGeneratorParagraph(paragraph = {}) {
  const snapshot = {
    id: paragraph.id,
    text: String(paragraph.text || ""),
  };
  if (Array.isArray(paragraph.runs)) snapshot.runs = paragraph.runs.map((run) => ({ ...run }));
  if (paragraph.alignment !== undefined) snapshot.alignment = paragraph.alignment;
  if (paragraph.font !== undefined) snapshot.font = paragraph.font;
  if (paragraph.fontSize !== undefined) snapshot.fontSize = paragraph.fontSize;
  return snapshot;
}

export function pushGeneratorHistory(history, paragraph, limit = GENERATOR_HISTORY_LIMIT) {
  if (!paragraph) return generatorHistory({ generatorHistory: history }).map((entry) => ({ ...entry }));
  const next = generatorHistory({ generatorHistory: history }).map((entry) => ({ ...entry }));
  next.push(snapshotGeneratorParagraph(paragraph));
  const max = Number(limit);
  if (Number.isFinite(max) && max > 0) {
    while (next.length > Math.floor(max)) next.shift();
  }
  return next;
}

export function generatorTouchedState(workstation) {
  if (workstation?.generatorMode === GENERATOR_MODE_GENERATED) return {};
  return { generatorDraftTouched: true };
}

export const GeneratorWorkstationModel = Object.freeze({
  GENERATOR_HISTORY_LIMIT,
  GENERATOR_MODE_DRAFT,
  GENERATOR_MODE_GENERATED,
  activeGeneratorParagraph,
  clearGeneratorState,
  cloneParagraph,
  cloneParagraphs,
  draftGeneratorState,
  generatedGeneratorState,
  generatorEditSnapshot,
  generatorExportReady,
  generatorHistory,
  generatorParagraphById,
  generatorParagraphs,
  generatorTouchedState,
  pushGeneratorHistory,
  snapshotGeneratorParagraph,
});
