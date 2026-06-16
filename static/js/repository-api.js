const RepositoryApi = (() => {
  const repositoryApiModule = import("./modules/repository-api.mjs?v=20260616xr1");
  const methodNames = [
    "deleteMatter",
    "driveStatus",
    "exportReviewDocx",
    "getMatter",
    "getMatterReview",
    "listMatters",
    "loadGmailStatus",
    "moveMatterToColumn",
    "saveMatterToDrive",
    "sendRedline",
    "syncGmail",
  ];

  function create({ reviewErrorFromPayload }) {
    const api = repositoryApiModule.then((module) => module.createRepositoryApi({ reviewErrorFromPayload }));
    return Object.fromEntries(methodNames.map((methodName) => [
      methodName,
      (...args) => api.then((implementation) => implementation[methodName](...args)),
    ]));
  }

  return { create };
})();
