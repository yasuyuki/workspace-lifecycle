export const title = "finish, a failing validator or preflight, and their corrections";
const work = (v) => {
  if (v.intent.kind === "none") return "作業中";
  if (v.intent.tree || v.intent.committed || v.intent.phase !== "none") return "停止: 効果を記録した finish の意図";
  return "停止: 効果のない finish の意図";
};
export const state = (v) => {
  if (v.accepted) return "受入済み";
  const where = v.pc !== "idle" ? "finish 実行中" : work(v);
  return where + (v.preflight === "bad" ? "（preflight が誤り）" : "");
};
export const group = (v) => (v.accepted ? "done" : v.pc === "idle" && v.intent.kind !== "none" ? "stopped" : "work");
export const colors = { work: "blue", stopped: "orange", done: "teal" };
export const step = (a, f, t) => {
  switch (a) {
    case "Start": return "owner が finish を実行";
    case "Enter": return t.pc === "idle" ? "finish が拒否" : t.intent.committed && !f.intent.committed ? "自分の commit を記録" : "finish が意図を保存";
    case "RestoreIntent": case "RestoreEffect": case "RestoreDone": return "生成物を restore";
    case "Stage": case "Tree": return "commit を準備";
    case "Commit": return "commit";
    case "Committed": return "commit を記録";
    case "Validate": return t.pc === "idle" ? "検証が失敗" : "検証が成功";
    case "Push": return t.pc === "idle" ? "preflight が失敗" : "preflight が push を許可";
    case "Record": return "push して受入";
    case "Kill": return "finish が強制終了";
    case "UpdateValidation": return t.intent.kind === "none" && f.intent.kind !== "none" ? "update-validation が意図を解放" : "update-validation";
    case "UpdatePreflight": return t.intent.kind === "none" && f.intent.kind !== "none" ? "update-preflight が意図を解放" : "update-preflight";
  }
  return a;
};
