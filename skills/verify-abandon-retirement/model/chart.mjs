export const title = "Retiring a task that will not be integrated";

export const state = (v) => {
  if (v.phase === "active") {
    if (v.integrated) return "Integrated";
    if (v.intent) return "Finish pending";
    if (v.held) return "Held";
    return "Working";
  }
  if (v.phase === "requested") return v.held ? "Abandonment requested, held" : "Abandonment requested";
  const kept = v.branch !== 99 && v.branch >= v.recorded;
  if (v.phase === "retired") return kept ? "Retired, branch keeps the commit" : "Retired, branch lost the commit";
  return "Reclaimed";
};

export const group = (v) => (v.phase === "active" ? "task" : v.phase === "reclaimed" ? "done" : "retiring");
export const colors = { task: "blue", retiring: "orange", done: "teal" };

export const step = (action) => ({
  UserCommits: "you commit",
  UserEdits: null,
  Hold: "a hold is placed or released",
  FinishStarts: "finish records an integration",
  FinishPushes: "finish pushes the integration",
  FinishWithdraws: "finish withdraws the integration",
  AbandonRequested: "retire --abandon [no hold, no pending finish]",
  Retires: "retire moves the checkout [branch at the recorded HEAD, nothing unsaved]",
  UserMovesBranch: "you delete or reset the branch",
  Reclaims: "reclaim removes the payload [branch keeps the commit]",
}[action]);
