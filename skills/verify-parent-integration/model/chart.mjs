export const title = "A child task integrates into its parent, then the parent into the default branch";
const has = (set, w) => Array.isArray(set) && set.includes(w);
const child = (v) => {
  if (has(v.cRecord, "c")) return "parent に統合済み";
  if (v.cIntent.open) return has(v.cIntent.merged, "c") ? "parent に merge 済み・未記録" : "parent への統合を開始";
  return "未統合";
};
const parent = (v) => {
  const acc = has(v.pAcc, "c") ? "受入に child あり" : "受入は parent だけ";
  return has(v.pHead, "c") && !has(v.pAcc, "c") ? acc + "・HEAD に未受入の merge" : acc;
};
const main = (v) => {
  if (has(v.dRemote, "c")) return "child まで push 済み";
  if (has(v.dRemote, "p")) return "parent だけ push 済み";
  return v.dIntent.length ? "統合を開始" : "未統合";
};
export const state = (v) => ({ "child": child(v), "parent": parent(v), "default branch": main(v) });
export const step = (a, f, t) => {
  const name = a.replace(/\(.*\)$/, "");
  const who = (x) => (x === "child" ? "child の finish" : "parent の finish");
  switch (name) {
    case "CStart": case "FStart": return null;
    case "CLock": return t.pc.child === "idle" ? "child の finish が拒否" : "child の finish が parent への統合を開始";
    case "CMerge": return t.pc.child === "idle" ? "child の finish が拒否" : "child を parent に merge";
    case "CMerged": return "merge を記録";
    case "CValidate": return "child と parent の validation";
    case "CPush": return "parent の topic へ push";
    case "CRecord": return "child の統合と parent の受入を保存";
    case "FEnter": return t.pc.parent === "idle" ? "parent の finish が拒否" : "parent の finish が受入";
    case "PStart": return null;
    case "PLock": return "default branch への統合を開始";
    case "PMerge": return "default checkout に merge";
    case "PPush": return "default branch へ push";
    case "PRecord": return "統合を記録";
    case "Kill": return "finish が強制終了";
    case "SessionCommits": return "parent の session が commit";
  }
  return a;
};
