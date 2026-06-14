// Browser loader that exposes the Playbook draft/publish modules as globals for
// playbook-view.js (which is a plain, non-module script). Mirrors the dynamic
// import() bridge used by repository-api.js.
//
// Because the modules load asynchronously, callers await `PlaybookRuntime.ready`
// before using `PlaybookRuntime.draft` / `PlaybookRuntime.api`.
const PlaybookRuntime = (() => {
  const draftModule = import("./modules/playbook-draft.mjs?v=20260614c");
  const apiModule = import("./modules/playbook-api.mjs?v=20260605a");
  const authoringModule = import("./modules/playbook-authoring-model.mjs?v=20260614c");

  const runtime = {
    draft: null,
    api: null,
    authoring: null,
    ready: Promise.all([draftModule, apiModule, authoringModule]).then(([draft, api, authoring]) => {
      runtime.draft = draft;
      runtime.api = api.createPlaybookApi({});
      runtime.authoring = authoring.PlaybookAuthoringModel;
      return runtime;
    }),
  };

  return runtime;
})();
