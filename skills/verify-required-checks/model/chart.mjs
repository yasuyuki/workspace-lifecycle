export const title = "finish --wait-checks: which checks it waits for";
const cannotAppear = (v) => {
  const gate = new Set([...v.required, ...v.headDecl.filter((c) => !v.required.includes(c))]);
  return [...gate].some((c) => !v.headJobs.includes(c)) || [...gate].some((c) => v.runs[c] === "none");
};
export const state = (v) => {
  if (v.integrated) return "統合済み";
  if (v.pc === "idle") {
    if (v.refused === "omit") return "拒否: 宣言された check を要求していない";
    if (v.refused === "absent") return "拒否: この commit に現れない check（merge を案内）";
    if (v.refused === "norun") return "拒否: workflow run が無い（再起動を案内）";
    if (v.refused === "pending") return "拒否: run が時間内に終わらない（待ち続けるよう案内）";
    if (v.pushed !== v.head) return v.head > 1 ? "作業中（default を merge 済み）" : "作業中";
    return cannotAppear(v) ? "受入済み・この commit に現れない check を要求" : "受入済み・check 待ち";
  }
  if (["pre", "resolve", "push"].includes(v.pc)) return "finish: plan を実行し topic へ push";
  if (v.pc === "integrate") return "統合: default の宣言を確認";
  if (v.pc === "wait") return "統合: check を待つ";
  return "統合: default へ push";
};
export const group = (v) => (v.integrated ? "done" : v.pc === "idle" && v.refused !== "none" ? "refused" : "work");
export const colors = { work: "blue", refused: "orange", done: "teal" };
export const step = (a, f, t) => {
  switch (a) {
    case "Start": return "agent が finish を実行";
    case "Pre": return t.pc === "integrate" ? "受入済みなので統合へ" : "plan を実行";
    case "Resolve": return "plan の restore が file を消す";
    case "Push": return t.lost > f.lost ? "push したが workflow run が作られない" : "topic へ push し CI が始まる";
    case "Integrate": return t.pc === "idle" ? "required checks omit で拒否" : "宣言を満たすので待機へ";
    case "Wait": return t.pc !== "idle" ? "check がすべて成功" : t.refused === "absent" ? "run は終わったが check が無い" : t.refused === "norun" ? "timeout（run が無い）" : "timeout（待ち続けるよう案内）";
    case "Final": return "default へ統合";
    case "Ci": return "CI の run が成功";
    case "Evolve": return "別 task が default に check と job を追加";
    case "Recover": return f.refused === "omit" ? "update-required-checks で宣言の check を要求" : f.refused === "absent" ? "案内どおり default を topic に merge" : f.refused === "norun" ? "案内どおり workflow を再起動" : "同じ finish をもう一度";
    case "Sync": return "agent が自分で default を topic に merge";
    case "Retrigger": return "agent が自分で workflow を再起動";
    case "Done": return null;
    default: return a;
  }
};
