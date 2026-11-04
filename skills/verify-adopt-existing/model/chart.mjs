export const title = "adopt-existing of one branch by request a, with a competing request b";
const request = (v, t) => {
  if (v.task[t] !== "none") return "task として採用";
  if (v.pc[t] !== "idle") return "adoption 実行中";
  if (v.intent[t] === "none") return "未登録";
  const stale = v.content !== v.intent[t] ? "、checkout が snapshot と不一致" : "";
  return (v.binding === t ? "中断（意図と binding あり" : "中断（意図あり") + stale + "）";
};
export const state = (v) => {
  const b = request(v, "b");
  const other = b === "task として採用" ? "（b が採用済み）" : "";
  return "a: " + request(v, "a") + other;
};
export const step = (a, f, t) => {
  const name = a.replace(/\(.*\)$/, "");
  switch (name) {
    case "Start": return "owner が adopt-existing を実行";
    case "Enter": case "Compare": case "Bind": case "Recheck":
      return Object.values(t.pc).every((p) => p === "idle") && t.lock === "none" ? "拒否" : "snapshot と binding を保存";
    case "Save": return t.task.a !== f.task.a ? "a の task を保存" : "b の task を保存";
    case "Kill": return "強制終了";
    case "WriterChanges": return "誰かが checkout を変更";
  }
  return a;
};
