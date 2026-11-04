export const title = "finish, a failing validator and update-validation";
const work = (v) => {
  if (v.accepted) return "受入済み";
  if (v.intent.kind === "none") return "作業中";
  if (v.intent.committed) return "commit 記録済み";
  if (v.head === "c1") return "commit 済み（未記録）";
  if (v.intent.tree) return "commit 準備済み";
  if (v.intent.phase !== "none") return v.restored ? "生成物を restore 済み" : "restore 中";
  return "finish の意図あり（効果なし）";
};
export const state = (v) => {
  const w = work(v);
  if (w === "受入済み" || w === "作業中") return w;
  if (v.pc !== "idle") return v.validator === "good" ? "finish 実行中（validator 更新済み）" : "finish 実行中";
  return "停止: " + w + (v.intent.committed && v.validator === "good" ? "（validator 更新済み）" : "");
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
    case "Record": return "push して受入";
    case "Kill": return "finish が強制終了";
    case "UpdateValidation": return t.intent.kind === "none" && f.intent.kind !== "none" ? "update-validation が意図を解放" : "update-validation";
  }
  return a;
};
