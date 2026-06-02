const RepositoryApi = (() => {
  const repositoryApiModule = import("./modules/repository-api.mjs");
  const methodNames = [
    "deleteMatter",
    "exportReviewDocx",
    "getMatter",
    "getMatterReview",
    "listMatters",
    "loadGmailStatus",
    "moveMatterToColumn",
    "sendRedline",
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
