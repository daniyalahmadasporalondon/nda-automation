// Browser loader that exposes the Playbook draft/publish modules as globals for
// playbook-view.js (which is a plain, non-module script). Mirrors the dynamic
// import() bridge used by repository-api.js.
//
// Because the modules load asynchronously, callers await `PlaybookRuntime.ready`
// before using `PlaybookRuntime.draft` / `PlaybookRuntime.api`.
const PlaybookRuntime = (() => {
  const draftModule = import("./modules/playbook-draft.mjs?v=20260605a");
  const apiModule = import("./modules/playbook-api.mjs?v=20260605a");

  const runtime = {
    draft: null,
    api: null,
    ready: Promise.all([draftModule, apiModule]).then(([draft, api]) => {
      runtime.draft = draft;
      runtime.api = api.createPlaybookApi({});
      return runtime;
    }),
  };

  return runtime;
})();
