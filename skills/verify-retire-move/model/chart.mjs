export const title = "retire moves one task checkout to recovery";
const retirement = (v) => {
  if (v.receipt !== "none") return "退役済み（receipt あり）";
  if (v.admin === "archive") return v.binding ? "admin を archive 済み" : "binding 解除済み";
  if (v.payload === "recovery") return "recovery へ移動済み";
  if (v.request === "moving") return "検査済み・移動前";
  if (v.request === "requested") return "退役依頼済み";
  return "退役依頼なし";
};
const checkout = (v) => {
  if (v.content === "dirty") return "未追跡 data あり";
  if (v.head === "moved") return "受入後の commit あり";
  return "受入 commit のまま";
};
export const state = (v) => ({
  "退役": retirement(v),
  "checkout": checkout(v),
  "利用": v.use === "running" ? "run で利用中" : "利用なし",
});
export const step = (a, f, t) => {
  switch (a) {
    case "AskInside": return "セッション内から finish --users-released";
    case "Start": return "retire を実行";
    case "Enter":
      return t.pc === "idle" ? "retire が拒否（branch が受入 commit でない）" : "retire が退役依頼を保存";
    case "Check":
      return t.pc === "idle" ? "retire が拒否（未追跡 data または HEAD の変更）" : "retire が内容を検査して移動を開始";
    case "Rename": case "Pointer": return "git worktree move";
    case "Held": return t.pc === "idle" ? "retire が拒否（移動先の Git 情報が不一致）" : "移動先を確認";
    case "Archive": return "admin を archive";
    case "Unbind": return t.pc === "idle" ? "retire が拒否（元の path に別の worktree）" : "branch の binding を解除";
    case "Receipt": return "receipt を保存";
    case "Kill": return "retire が強制終了";
    case "RunStarts": return "run を開始";
    case "RunEnds": return "run が終了";
    case "SessionWrites":
      if (t.content !== f.content) return t.content === "dirty" ? "セッションが未追跡 data を書く" : "セッションが未追跡 data を消す";
      return t.head === "moved" ? "セッションが commit" : "セッションが受入 commit へ戻す";
    case "OtherBegins": return "別 task が元の path に begin";
    case "Done": return null;
  }
  return a;
};
