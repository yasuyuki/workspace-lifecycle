const session = (v) => {
  const pc = v.pc.s1, child = v.child.s1, lease = v.receipt.s1 !== "none";
  if (pc === "crashed") {
    if (child) return lease ? "supervisor 異常終了・lease 残存・コマンド実行中" : "lease 解放済み・コマンド実行中";
    return lease ? "supervisor 異常終了・lease 残存" : "未実行";
  }
  if (pc === "idle") return "未実行";
  if (["probe", "share", "save", "job", "spawn"].includes(pc)) return "lease を登録中";
  if (pc === "record") return child ? "実行中（子の記録前）" : "lease を登録中";
  return child ? "実行中" : "終了処理中";
};
const change = (v) => {
  if (v.outPc === "mutating") return "外から変更中";
  if (v.outPc !== "idle") return "外からの変更を確認中";
  if (v.inPc.s1 === "mutating") return "セッション内から変更中";
  if (v.inPc.s1 !== "idle") return "セッション内からの変更を確認中";
  if (v.relPc !== "idle") return "lease-release 実行中";
  return "変更なし";
};
export const title = "run lease と変更操作(1 セッション)";
export const state = (v) => ({ "セッション": session(v), "変更操作": change(v) });
export const step = (a, f, t) => {
  switch (a) {
    case "RunRegister": case "RunShare": case "RunSaveReceipt": case "RunSaveJob": return "run が lease を登録";
    case "RunProbe": return t.pc.s1 === "idle" ? "run が変更中のため拒否される" : "run が lease を登録";
    case "RunSpawn": return "コマンドを起動";
    case "RunRecordChild": return "子の PID を lease に記録";
    case "ChildExits": return "コマンドが終了";
    case "RunComplete": case "RunUnlock": return "lease を外して終了";
    case "SupervisorCrashes": return "supervisor が強制終了";
    case "OutEnter": case "OutReadUses": return "外から finish・retire などを開始";
    case "OutExclusive": return t.outPc === "mutating" ? "使用者なしと判定" : "使用中として拒否";
    case "OutDone": case "InDone": return "変更が完了";
    case "InEnter": return "コマンド内から finish などを開始";
    case "InDecide": return t.inPc.s1 === "mutating" ? "自分だけの使用と判定" : "使用中として拒否";
    case "RelEnter": case "RelPick": return "lease-release を実行";
    case "RelCheck": return t.receipt.s1 === "none" && f.receipt.s1 !== "none" ? "lease を解放" : "生存中として拒否";
  }
  return a;
};
