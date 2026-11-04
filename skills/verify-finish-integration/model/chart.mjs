const has = (set, x) => Object.values(set).includes(x);
export const title = "finish and required checks, one task into the default branch";
export const state = (v) => {
  if (v.integrated === v.head && v.intent.kind === "none") return "統合済み";
  if (v.intent.kind === "merge") {
    if (v.intent.checks) return "check 成功、統合先から push 中";
    if (v.pc === "await") return "CI 待ち（統合先で準備済み・未 push）";
    if (v.pc === "withdraw" || v.pc === "withdrawPop") return "準備した統合を取り下げ中";
    if (v.pc === "idle") return v.head === v.intent.src ? "準備した統合が残る（finish なし）" : "準備した統合が残り HEAD は修正 commit（finish は拒否）";
    return "統合先で準備中";
  }
  if (v.rec.kind === "candidate" && v.rec.commit === v.head) return v.ci[v.head] === "success" ? "候補（check 成功）" : "候補（check 未成功）";
  return "作業中";
};
export const group = (v) => {
  const s = state(v);
  if (s === "統合済み") return "done";
  if (s.startsWith("候補")) return "candidate";
  if (s === "作業中") return "work";
  return "integrating";
};
export const colors = { work: "blue", candidate: "yellow", integrating: "purple", done: "teal" };
export const step = (a, f, t) => {
  switch (a) {
    case "Start": return t.mode === "wait" ? "owner が finish --wait-checks を実行" : "owner が finish を実行";
    case "FinEnter": return t.pc === "idle" ? "finish が拒否" : "finish が意図を保存";
    case "FinValidate": return "finish が検証（候補は省略）";
    case "FinPush": return "finish が topic を push";
    case "FinRecord": return t.rec.kind === "candidate" ? "候補を記録" : "受入を記録";
    case "IntVerify": return t.pc === "withdraw" ? "check 未成功で取り下げ" : t.pc === "idle" ? "check 未成功で拒否" : "finish が check を確認";
    case "IntLock": return "統合先で統合を開始";
    case "IntGate": return t.pc === "await" ? "統合先で検証、check を待つ" : "check 成功を記録";
    case "Await": return t.pc === "withdraw" ? "check 失敗・期限切れ" : "check 成功、もう一度統合";
    case "Withdraw": return "統合先を元の HEAD に戻す";
    case "WithdrawPop": return "取り下げ完了";
    case "IntRecord": return "統合先を push し記録";
    case "OwnerFix": return "owner が修正を commit";
    case "CiCompletes": return t.ci[f.head] === "success" ? "CI 成功" : "CI 失敗";
    case "CiRerun": return "check を再実行";
    case "Kill": return "finish が強制終了";
  }
  return a;
};
