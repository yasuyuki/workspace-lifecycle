export const title = "reclaim deletes the retired payload";
export const state = (v) => {
  if (v.phase === "payload-removed") return "payload 回収済み";
  if (v.held) return "拒否して保持";
  if (v.dirAt === "gone") return "payload directory 削除済み";
  if (v.mStatus === "removed") return "file 削除済み、directory 残存";
  if (v.mStatus === "quarantined" && v.tomb === "none" && v.name === "none") return "file 削除済み（未記録）";
  if (v.tomb !== "none") return v.mStatus === "quarantined" ? "file を検査済み（quarantine 名）" : "file を quarantine 名へ移動";
  if (v.dirAt === "rtomb") return "directory を quarantine 名へ移動";
  if (v.dirAt === "stg") return v.phase === "renamed" ? "staging で検査済み" : "staging へ移動";
  if (v.phase === "authorized") return "回収を許可（recovery に保持）";
  return v.name === "o1" && v.content.o1 === "orig" ? "回収依頼あり（recovery に保持）" : "回収依頼あり、recovery が変更された";
};
export const group = (v) => {
  const s = state(v);
  if (s === "拒否して保持") return "held";
  if (s.endsWith("回収済み")) return "done";
  if (s.startsWith("回収")) return "kept";
  return "removing";
};
export const colors = { kept: "blue", removing: "purple", held: "orange", done: "teal" };
export const step = (a, f, t) => {
  if (t.held) return "変更を見つけて拒否";
  switch (a) {
    case "Auth": return "capture が一致し回収を許可";
    case "StageRename": return "recovery を staging へ rename";
    case "StagedCheck": return "staging の manifest が一致";
    case "RootRename": return "directory を quarantine 名へ rename";
    case "Rename": return "file を quarantine 名へ rename";
    case "CheckId": return "identity が一致";
    case "Unlink": case "WDelete": return "file を削除";
    case "Member": case "WMember": return "削除済みの file を確認";
    case "Rmdir": return "空の directory を削除";
    case "Finish": return "回収済みを記録";
    case "Crash": return null;
    case "Start": return "reclaim を再実行";
    case "StageCheck": return "recovery の manifest が一致";
    case "Scan": return "capture が一致";
    case "Removed": return "削除を記録";
    case "WriterEdits": case "WriterReplaces": case "WriterDeletes": case "WriterCreates": return "誰かが recovery を変更";
  }
  return a;
};
