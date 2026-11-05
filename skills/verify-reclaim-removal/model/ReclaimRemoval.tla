---------------------------- MODULE ReclaimRemoval ----------------------------
(* The deletion steps of reclaim(): service.py reclaim() renames the recovery
   payload to its staging name and checks it, then reclamation.remove_tree()
   removes each captured member and the root, on Linux by quarantine renames
   and on Windows through one restrictive handle. The payload holds one
   captured file. One action is one saved progress record or one filesystem
   effect. The reclaim process can crash at any step and run again, and a
   writer can change the file under the names it knows. *)
EXTENDS Naturals, FiniteSets

CONSTANTS
  Windows,    \* TRUE: _remove_tree_windows; FALSE: the Linux quarantine path
  Writer,     \* "none"; "contract": writes only under the recovery path, before
              \* reclaim renames it; "stray": also under the staging path
  MaxWrites

Objects == {"o1", "o2"}   \* o1: the captured file; o2: a file a writer puts in its place
None == "none"
HandleHeld == {"wcheck", "wdelete"}

VARIABLES
  dirAt,       \* where the payload directory is: rec, stg (staging), rtomb (Linux root quarantine), gone
  name,        \* the object under the member's own name
  tomb,        \* the object under the member's quarantine name (Linux)
  content,     \* content[o]: orig (the captured bytes) or changed
  phase,       \* receipt reclaim_phase: requested, authorized, renamed, payload-removed
  rootIntent,  \* progress root intent (Linux root rename)
  rootRmIntent,\* progress root remove_intent
  mIntent,     \* progress member intent
  mStatus,     \* progress member status: none, quarantined, removed
  pc,
  deleted,     \* <<object, content>> of each object reclaim deleted
  writes,
  refused,     \* reclaim has refused at least once
  held         \* the last reclaim run ended in a refusal

vars == <<dirAt, name, tomb, content, phase, rootIntent, rootRmIntent, mIntent, mStatus,
          pc, deleted, writes, refused, held>>
fs == <<dirAt, name, tomb, content>>
progress == <<phase, rootIntent, rootRmIntent, mIntent, mStatus>>

Init ==
  /\ dirAt = "rec" /\ name = "o1" /\ tomb = None
  /\ content = [o \in Objects |-> "orig"]
  /\ phase = "requested" /\ rootIntent = FALSE /\ rootRmIntent = FALSE
  /\ mIntent = FALSE /\ mStatus = None
  /\ pc = "idle" /\ deleted = {} /\ writes = 0 /\ refused = FALSE /\ held = FALSE

\* The bytes under the name match the content manifest (_tree_manifest hashes content).
ManifestMatches == name # None /\ content[name] = "orig" /\ tomb = None
\* A capture also matches the identity (reclamation.capture, same_capture).
CaptureMatches == name = "o1" /\ content["o1"] = "orig" /\ tomb = None

Refuse == pc' = "idle" /\ refused' = TRUE /\ held' = TRUE
Go(next) == pc' = next /\ UNCHANGED refused /\ held' = FALSE

----------------------------------------------------------------------------
(* service.py reclaim() *)

Start ==
  /\ pc = "idle" /\ phase # "payload-removed"
  /\ Go("auth")
  /\ UNCHANGED <<fs, progress, deleted, writes>>

\* Lines 2306-2322: phase requested captures the payload and authorizes.
Auth ==
  /\ pc = "auth"
  /\ IF phase = "requested"
     THEN IF dirAt = "rec" /\ CaptureMatches
          THEN phase' = "authorized" /\ Go("stageCheck") ELSE Refuse /\ UNCHANGED phase
     ELSE UNCHANGED phase /\ Go(IF phase = "authorized" THEN "stageCheck" ELSE "remove")
  /\ UNCHANGED <<fs, rootIntent, rootRmIntent, mIntent, mStatus, deleted, writes>>

\* Lines 2323-2334: before the rename, the payload must match its manifest.
StageCheck ==
  /\ pc = "stageCheck"
  /\ CASE dirAt = "rec" /\ ManifestMatches -> Go("stageRename")
       [] dirAt = "stg" -> Go("stagedCheck")
       [] OTHER -> Refuse
  /\ UNCHANGED <<fs, progress, deleted, writes>>

\* Line 2328: recovery.rename(staging).
StageRename ==
  /\ pc = "stageRename"
  /\ dirAt' = "stg" /\ Go("stagedCheck")
  /\ UNCHANGED <<name, tomb, content, progress, deleted, writes>>

\* Lines 2335-2344: after the rename, the staging tree must match its manifest.
StagedCheck ==
  /\ pc = "stagedCheck"
  /\ IF dirAt = "stg" /\ ManifestMatches
     THEN phase' = "renamed" /\ Go("remove") ELSE Refuse /\ UNCHANGED phase
  /\ UNCHANGED <<fs, rootIntent, rootRmIntent, mIntent, mStatus, deleted, writes>>

----------------------------------------------------------------------------
(* reclamation.remove_tree() *)

\* Linux 603-618 (_resolve_root) and Windows 480-486: a root already gone is
\* accepted only after every member removal and the root removal intent.
Remove ==
  /\ pc = "remove"
  /\ IF dirAt = "gone"
     THEN IF rootRmIntent /\ mStatus = "removed" THEN Go("finish") ELSE Refuse
     ELSE Go(IF Windows THEN "wmember" ELSE IF dirAt = "stg" THEN "rootIntent" ELSE "scan")
  /\ UNCHANGED <<fs, progress, deleted, writes>>

\* Linux 619-621: root rename intent.
RootIntent ==
  /\ pc = "rootIntent"
  /\ rootIntent' = TRUE /\ Go("rootRename")
  /\ UNCHANGED <<fs, phase, rootRmIntent, mIntent, mStatus, deleted, writes>>

\* Linux 623: os.rename(original, staging) into the root quarantine name.
RootRename ==
  /\ pc = "rootRename"
  /\ dirAt' = "rtomb" /\ Go("scan")
  /\ UNCHANGED <<name, tomb, content, progress, deleted, writes>>

\* Linux 633-637: without member intent the capture must match; with it, only
\* the names are checked (_validate_resuming_names).
Scan ==
  /\ pc = "scan"
  /\ IF ~mIntent
     THEN IF CaptureMatches THEN Go("member") ELSE Refuse
     ELSE IF (name # None /\ tomb # None) \/ (mStatus = "removed" /\ (name # None \/ tomb # None))
          THEN Refuse ELSE Go("member")
  /\ UNCHANGED <<fs, progress, deleted, writes>>

\* Linux 646-683: per-member state from the two names.
Member ==
  /\ pc = "member"
  /\ CASE mStatus = "removed" -> Go("rootRmIntent") /\ UNCHANGED mStatus
       [] name # None /\ tomb # None -> Refuse /\ UNCHANGED mStatus
       [] name = None /\ tomb = None ->
            IF mIntent THEN mStatus' = "removed" /\ Go("rootRmIntent")
            ELSE Refuse /\ UNCHANGED mStatus
       [] tomb = None -> Go("intent") /\ UNCHANGED mStatus
       [] OTHER -> Go("checkId") /\ UNCHANGED mStatus
  /\ UNCHANGED <<fs, phase, rootIntent, rootRmIntent, mIntent, deleted, writes>>

\* Linux 684-685: member intent before the rename.
Intent ==
  /\ pc = "intent"
  /\ mIntent' = TRUE /\ Go("rename")
  /\ UNCHANGED <<fs, phase, rootIntent, rootRmIntent, mStatus, deleted, writes>>

\* Linux 687: os.rename(name, tomb); a missing name raises.
Rename ==
  /\ pc = "rename"
  /\ IF name = None THEN Refuse /\ UNCHANGED <<name, tomb>>
     ELSE tomb' = name /\ name' = None /\ Go("checkId")
  /\ UNCHANGED <<dirAt, content, progress, deleted, writes>>

\* Linux 692-696: the quarantined object must be the captured one.
CheckId ==
  /\ pc = "checkId"
  /\ IF tomb = "o1" THEN mStatus' = "quarantined" /\ Go("digest")
     ELSE Refuse /\ UNCHANGED mStatus
  /\ UNCHANGED <<fs, phase, rootIntent, rootRmIntent, mIntent, deleted, writes>>

\* Linux 698-706: identity, links and digest on its own descriptor.
Digest ==
  /\ pc = "digest"
  /\ IF tomb = "o1" /\ content["o1"] = "orig" THEN Go("unlink") ELSE Refuse
  /\ UNCHANGED <<fs, progress, deleted, writes>>

\* Linux 709: os.unlink(tomb).
Unlink ==
  /\ pc = "unlink"
  /\ deleted' = deleted \cup {<<tomb, content[tomb]>>}
  /\ tomb' = None /\ Go("removed")
  /\ UNCHANGED <<dirAt, name, content, progress, writes>>

\* Linux 724-725 and Windows 556: the member is removed.
Removed ==
  /\ pc = "removed"
  /\ mStatus' = "removed" /\ Go("rootRmIntent")
  /\ UNCHANGED <<fs, phase, rootIntent, rootRmIntent, mIntent, deleted, writes>>

\* Linux 728-729 and Windows 558-559.
RootRmIntent ==
  /\ pc = "rootRmIntent"
  /\ rootRmIntent' = TRUE /\ Go("rmdir")
  /\ UNCHANGED <<fs, phase, rootIntent, mIntent, mStatus, deleted, writes>>

\* Linux 744 os.rmdir and Windows 564-570: only an empty root goes.
Rmdir ==
  /\ pc = "rmdir"
  /\ IF name = None /\ tomb = None THEN dirAt' = "gone" /\ Go("finish")
     ELSE Refuse /\ UNCHANGED dirAt
  /\ UNCHANGED <<name, tomb, content, progress, deleted, writes>>

\* service.py 2352-2353: phase payload-removed.
Finish ==
  /\ pc = "finish"
  /\ phase' = "payload-removed" /\ Go("idle")
  /\ UNCHANGED <<fs, rootIntent, rootRmIntent, mIntent, mStatus, deleted, writes>>

----------------------------------------------------------------------------
(* Windows: _remove_tree_windows_bound() *)

\* Windows 506-522: unknown names refuse; a missing member needs its intent.
WMember ==
  /\ pc = "wmember"
  /\ CASE mStatus = "removed" /\ name = None -> Go("rootRmIntent") /\ UNCHANGED mStatus
       [] name = None ->
            IF mIntent THEN mStatus' = "removed" /\ Go("rootRmIntent")
            ELSE Refuse /\ UNCHANGED mStatus
       [] mStatus = "removed" -> Refuse /\ UNCHANGED mStatus
       [] OTHER -> Go("wintent") /\ UNCHANGED mStatus
  /\ UNCHANGED <<fs, phase, rootIntent, rootRmIntent, mIntent, deleted, writes>>

\* Windows 523-524: member intent.
WIntent ==
  /\ pc = "wintent"
  /\ mIntent' = TRUE /\ Go("wopen")
  /\ UNCHANGED <<fs, phase, rootIntent, rootRmIntent, mStatus, deleted, writes>>

\* Windows 530: _win_open with DELETE and read-only sharing; writers are shut out from here.
WOpen ==
  /\ pc = "wopen"
  /\ IF name = None THEN Refuse ELSE Go("wcheck")
  /\ UNCHANGED <<fs, progress, deleted, writes>>

\* Windows 531-545: identity, links and hash through the handle.
WCheck ==
  /\ pc = "wcheck"
  /\ IF name = "o1" /\ content["o1"] = "orig"
     THEN mStatus' = "quarantined" /\ Go("wdelete")
     ELSE Refuse /\ UNCHANGED mStatus
  /\ UNCHANGED <<fs, phase, rootIntent, rootRmIntent, mIntent, deleted, writes>>

\* Windows 546-550: delete disposition on the handle, applied when it closes.
WDelete ==
  /\ pc = "wdelete"
  /\ deleted' = deleted \cup {<<name, content[name]>>}
  /\ name' = None /\ Go("removed")
  /\ UNCHANGED <<dirAt, tomb, content, progress, writes>>

----------------------------------------------------------------------------
(* Everything else *)

\* The reclaim process dies; a Windows handle closes, progress records stay.
Crash ==
  /\ pc # "idle"
  /\ pc' = "idle"
  /\ UNCHANGED <<fs, progress, deleted, writes, refused, held>>

WriterReaches ==
  /\ writes < MaxWrites /\ pc \notin HandleHeld
  /\ \/ Writer = "contract" /\ dirAt = "rec"
     \/ Writer = "stray" /\ dirAt \in {"rec", "stg"}

WriterEdits ==
  /\ WriterReaches /\ name # None
  /\ content' = [content EXCEPT ![name] = "changed"]
  /\ writes' = writes + 1
  /\ UNCHANGED <<dirAt, name, tomb, progress, pc, deleted, refused, held>>

\* An editor saves the same bytes as a new file under the name.
WriterReplaces ==
  /\ WriterReaches /\ name = "o1" /\ tomb # "o2"
  /\ name' = "o2" /\ content' = [content EXCEPT !["o2"] = content["o1"]]
  /\ writes' = writes + 1
  /\ UNCHANGED <<dirAt, tomb, progress, pc, deleted, refused, held>>

WriterDeletes ==
  /\ WriterReaches /\ name # None
  /\ name' = None /\ writes' = writes + 1
  /\ UNCHANGED <<dirAt, tomb, content, progress, pc, deleted, refused, held>>

WriterCreates ==
  /\ WriterReaches /\ name = None /\ tomb # "o2"
  /\ name' = "o2" /\ content' = [content EXCEPT !["o2"] = "changed"]
  /\ writes' = writes + 1
  /\ UNCHANGED <<dirAt, tomb, progress, pc, deleted, refused, held>>

Done ==
  /\ pc = "idle" /\ (phase = "payload-removed" \/ refused)
  /\ UNCHANGED vars

Next ==
  \/ Start \/ Auth \/ StageCheck \/ StageRename \/ StagedCheck
  \/ Remove \/ RootIntent \/ RootRename \/ Scan \/ Member \/ Intent \/ Rename
  \/ CheckId \/ Digest \/ Unlink \/ Removed \/ RootRmIntent \/ Rmdir \/ Finish
  \/ WMember \/ WIntent \/ WOpen \/ WCheck \/ WDelete
  \/ Crash \/ WriterEdits \/ WriterReplaces \/ WriterDeletes \/ WriterCreates \/ Done

Spec == Init /\ [][Next]_vars

----------------------------------------------------------------------------
(* Rules *)

\* Reclaim deletes only the captured object with its captured bytes.
OnlyCapturedBytesDeleted == deleted \subseteq {<<"o1", "orig">>}

\* A member is recorded as removed only once the captured object is gone.
RemovedMeansGone == mStatus = "removed" => name # "o1" /\ tomb # "o1"

\* The payload directory is gone only once it is empty.
RootGoneOnlyEmpty == dirAt = "gone" => name = None /\ tomb = None

\* Without a writer, crashes and reruns never strand reclaim in a refusal.
NoRefusalWithoutWriter == writes = 0 => ~refused
=============================================================================
