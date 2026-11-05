export const title = "finish, its dirt checks and the files a validator leaves";
const where = (v) => (v.kind === "parent" ? "parent" : "main");
const names = { valout: "validator の生成物", manual: "手で実行した test の生成物", foreign: "別 session の private file" };
export const state = (v) => {
  if (v.integrated) return "統合済み";
  if (v.pc === "idle") {
    if (v.refused.length) return "拒否: " + v.refused.map((p) => names[p]).join("、");
    if (v.accepted) return "受入済み・統合待ち";
    if (!v.writes) return "作業中（validator は書かない）";
    return v.declared ? "作業中（生成物を宣言済み）" : "作業中（validator が生成物を書く）";
  }
  if (["pre", "resolve", "commit"].includes(v.pc)) return "finish: plan を確認・commit";
  if (["validate", "clean", "post"].includes(v.pc)) return "finish: task checkout で validation";
  return "統合: " + where(v) + " で validation";
};
export const group = (v) => (v.integrated ? "done" : v.pc === "idle" && v.refused.length ? "refused" : "work");
export const colors = { work: "blue", refused: "orange", done: "teal" };
export const step = (a, f, t) => {
  switch (a) {
    case "Start": return "agent が finish を実行";
    case "Pre": return t.pc === "idle" ? "finish が既存の dirt を拒否" : t.pc === "integrate" ? "受入済みなので統合へ進む" : "dirt は plan が覆う";
    case "Resolve": return "plan の archive を移す";
    case "Commit": return "commit";
    case "Validate": return t.made.length ? "validator が生成物を書く" : "validation が通る";
    case "Clean": case "ItClean": return t.cleaned.length > f.cleaned.length ? "宣言した生成物を消す" : "消すものはない";
    case "Post": return t.pc === "idle" ? "finish が validation 後の dirt を拒否" : "finish が受け入れる";
    case "Integrate": return t.pc === "idle" ? "統合先の dirt を拒否" : "統合先へ merge";
    case "ItValidate": return t.made.length > f.made.length ? "validator が統合先に生成物を書く" : "統合先で validation が通る";
    case "ItCheck": return t.integrated ? "統合する" : "integration changed during final validation";
    case "Recover": return t.declared && !f.declared ? "agent が生成物を宣言し、名指しされた file を消す" : "agent が名指しされた file を消す";
    case "Manual": return "agent が test を手で実行";
    case "UpdateValidation": return "update-validation で生成物を作らない validator に替える";
    case "Foreign": return "別 session が private file を書く";
    case "Done": return null;
    default: return a;
  }
};
