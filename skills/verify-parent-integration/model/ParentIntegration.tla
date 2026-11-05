-------------------------- MODULE ParentIntegration --------------------------
(* service.py _integrate() for a child task whose integration target is a
   parent task, followed by the parent's own edge to the remote default
   branch. A commit is the set of works it contains: "p" the parent's
   accepted work, "c" the child's accepted work, "m" a later commit by the
   parent's session. Two processes run: the child's finish, which integrates
   the child into the parent and then the parent into the default branch, and
   the parent's finish, which accepts the parent's HEAD and integrates it.
   Either can be killed at any step and run again. *)
EXTENDS Naturals, FiniteSets

CONSTANTS
  Writer,     \* "none"; "session": the parent's session commits once while no lifecycle process holds the parent
  MaxKills,
  ParentRuns,     \* TRUE: the parent's own finish also runs
  ParentEdgeKills, \* TRUE: a kill can also fall inside the parent's edge to the default branch
  ParentIsDependency \* TRUE: the child also names the parent with --dependency

Procs == {"child", "parent"}
None == "none"
NoIntent == [target |-> {}, merged |-> {}, open |-> FALSE]

VARIABLES
  pHead,      \* the parent branch in the parent's checkout
  pRemote,    \* the parent's topic on the remote
  pAcc,       \* the parent's saved acceptance commit
  pOwn,       \* the parent works that a parent finish accepted
  cIntent,    \* the child's saved merge intent into the parent: target_head and merged
  cChecked,   \* the child and parent validations ran on a parent commit with "c"
  cRecord,    \* the child's saved integration record (its merged commit), or {}
  dIntent,    \* the parent's saved merge intent into the default branch: its source, or {}
  dLocal,     \* the default branch in the default checkout
  dRemote,    \* the remote default branch
  dRecord,    \* the parent's saved integration record: its source, or {}
  accepted,   \* every value pAcc has had
  lockP,      \* the process holding the parent's lease, or none
  lockD,      \* the process holding the default branch lease, or none
  pc,
  kills,
  commits

vars == <<pHead, pRemote, pAcc, pOwn, cIntent, cChecked, cRecord, dIntent, dLocal, dRemote, dRecord,
          accepted, lockP, lockD, pc, kills, commits>>

Init ==
  /\ pHead = {"p"} /\ pRemote = {"p"} /\ pAcc = {"p"} /\ pOwn = {"p"}
  /\ cIntent = NoIntent /\ cChecked = FALSE /\ cRecord = {}
  /\ dIntent = {} /\ dLocal = {} /\ dRemote = {} /\ dRecord = {}
  /\ accepted = {{"p"}}
  /\ lockP = None /\ lockD = None
  /\ pc = [x \in Procs |-> "idle"] /\ kills = 0 /\ commits = 0

Go(x, step) == pc' = [pc EXCEPT ![x] = step]
Stop(x) == pc' = [pc EXCEPT ![x] = "idle"]
\* A refusal raises out of every with-block: the process's leases are dropped.
Release(x) ==
  /\ lockP' = IF lockP = x THEN None ELSE lockP
  /\ lockD' = IF lockD = x THEN None ELSE lockD
Refuse(x) == Stop(x) /\ Release(x)

----------------------------------------------------------------------------
(* The child's edge into the parent: _integrate(child). *)

\* _edge_leases(): the child's lease and the parent's, without allow_use for the parent.
CStart ==
  /\ pc["child"] = "idle" /\ lockP = None
  /\ lockP' = "child" /\ Go("child", "cLock")
  /\ UNCHANGED <<pHead, pRemote, pAcc, pOwn, cIntent, cChecked, cRecord, dIntent, dLocal, dRemote, dRecord, accepted, lockD, kills, commits>>

(* Lines 1632-1716: a dependency's branch must be at its accepted commit
   (1645-1651); an existing record must still be in the parent's HEAD;
   otherwise a new intent needs the parent HEAD at its accepted commit. *)
CLock ==
  /\ pc["child"] = "cLock"
  /\ IF ParentIsDependency /\ pHead # pAcc
     THEN Refuse("child") /\ UNCHANGED cIntent
     ELSE IF cRecord # {}
     THEN IF cRecord \subseteq pHead
          THEN Go("child", "pStart") /\ lockP' = None /\ UNCHANGED <<cIntent, lockD>>
          ELSE Refuse("child") /\ UNCHANGED cIntent
     ELSE IF cIntent.open
          THEN Go("child", "cMerge") /\ UNCHANGED <<cIntent, lockP, lockD>>
          ELSE IF pHead = pAcc
               THEN /\ cIntent' = [target |-> pHead, merged |-> {}, open |-> TRUE]
                    /\ Go("child", "cMerge") /\ UNCHANGED <<lockP, lockD>>
               ELSE Refuse("child") /\ UNCHANGED cIntent
  /\ UNCHANGED <<pHead, pRemote, pAcc, pOwn, cChecked, cRecord, dIntent, dLocal, dRemote, dRecord, accepted, kills, commits>>

(* Lines 1730-1785: at target_head, merge the child (fast-forward, or a
   --no-ff merge committed at once); a HEAD that is exactly that merge is
   adopted; any other HEAD is refused. *)
CMerge ==
  /\ pc["child"] = "cMerge"
  /\ IF cIntent.merged # {}
     THEN IF pHead = cIntent.merged
          THEN Go("child", "cValidate") /\ UNCHANGED <<pHead, cIntent, lockP>>
          ELSE Refuse("child") /\ UNCHANGED <<pHead, cIntent>>
     ELSE IF pHead = cIntent.target
          THEN /\ pHead' = pHead \cup {"c"}
               /\ Go("child", "cMerged") /\ UNCHANGED <<cIntent, lockP>>
          ELSE IF pHead = cIntent.target \cup {"c"}
               THEN /\ cIntent' = [cIntent EXCEPT !.merged = pHead]
                    /\ Go("child", "cValidate") /\ UNCHANGED <<pHead, lockP>>
               ELSE Refuse("child") /\ UNCHANGED <<pHead, cIntent>>
  /\ UNCHANGED <<pRemote, pAcc, pOwn, cChecked, cRecord, dIntent, dLocal, dRemote, dRecord, accepted, lockD, kills, commits>>

\* intent['merged'] saved after the merge commit.
CMerged ==
  /\ pc["child"] = "cMerged"
  /\ cIntent' = [cIntent EXCEPT !.merged = pHead]
  /\ Go("child", "cValidate")
  /\ UNCHANGED <<pHead, pRemote, pAcc, pOwn, cChecked, cRecord, dIntent, dLocal, dRemote, dRecord, accepted, lockP, lockD, kills, commits>>

(* Lines 1795-1811: the child's and the parent's validation in the parent
   checkout; HEAD must still be the merged commit. *)
CValidate ==
  /\ pc["child"] = "cValidate"
  /\ IF pHead = cIntent.merged
     THEN cChecked' = TRUE /\ Go("child", "cPush") /\ UNCHANGED lockP
     ELSE Refuse("child") /\ UNCHANGED cChecked
  /\ UNCHANGED <<pHead, pRemote, pAcc, pOwn, cIntent, cRecord, dIntent, dLocal, dRemote, dRecord, accepted, lockD, kills, commits>>

\* Line 1825: _push of the parent branch with expected_head=merged.
CPush ==
  /\ pc["child"] = "cPush"
  /\ pRemote' = cIntent.merged /\ Go("child", "cRecord")
  /\ UNCHANGED <<pHead, pAcc, pOwn, cIntent, cChecked, cRecord, dIntent, dLocal, dRemote, dRecord, accepted, lockP, lockD, kills, commits>>

(* Lines 1826-1837: the child's integrated record and the parent's new
   acceptance in one save; the intent is popped and the leases released. *)
CRecord ==
  /\ pc["child"] = "cRecord"
  /\ cRecord' = cIntent.merged
  /\ pAcc' = cIntent.merged /\ accepted' = accepted \cup {cIntent.merged}
  /\ cIntent' = NoIntent
  /\ Go("child", "pStart") /\ lockP' = None
  /\ UNCHANGED <<pHead, pRemote, pOwn, cChecked, dIntent, dLocal, dRemote, dRecord, lockD, kills, commits>>

----------------------------------------------------------------------------
(* The parent's finish. *)

\* finish() takes the parent's lease (allow_use) and the state lock.
FStart ==
  /\ ParentRuns /\ pc["parent"] = "idle" /\ lockP = None
  /\ lockP' = "parent" /\ Go("parent", "fEnter")
  /\ UNCHANGED <<pHead, pRemote, pAcc, pOwn, cIntent, cChecked, cRecord, dIntent, dLocal, dRemote, dRecord, accepted, lockD, kills, commits>>

(* Line 986 _refuse_pending_merge(state, branch); with the parent's own merge
   intent pending, the HEAD must be the accepted commit (1042-1044); else a new
   HEAD is validated, pushed and accepted (verify-validation-update covers its
   steps); the finish lease is released before integration. *)
FEnter ==
  /\ pc["parent"] = "fEnter"
  /\ IF cIntent.open \/ (dIntent # {} /\ pHead # pAcc)
     THEN Refuse("parent") /\ UNCHANGED <<pRemote, pAcc, pOwn, accepted>>
     ELSE /\ IF pHead # pAcc
             THEN /\ pAcc' = pHead /\ pRemote' = pHead /\ pOwn' = pOwn \cup (pHead \ {"c"})
                  /\ accepted' = accepted \cup {pHead}
             ELSE UNCHANGED <<pRemote, pAcc, pOwn, accepted>>
          /\ Go("parent", "pStart") /\ lockP' = None /\ UNCHANGED lockD
  /\ UNCHANGED <<pHead, cIntent, cChecked, cRecord, dIntent, dLocal, dRemote, dRecord, kills, commits>>

----------------------------------------------------------------------------
(* The parent's edge into the default branch: _integrate(parent), from the
   child's recursion (line 1840) or the parent's finish. *)

PStart(x) ==
  /\ pc[x] = "pStart" /\ lockP = None /\ lockD = None
  /\ lockP' = x /\ lockD' = x /\ Go(x, "pLock")
  /\ UNCHANGED <<pHead, pRemote, pAcc, pOwn, cIntent, cChecked, cRecord, dIntent, dLocal, dRemote, dRecord, accepted, kills, commits>>

(* Lines 1632-1721: the parent branch must be at its accepted commit; an
   existing record of that commit ends the edge; a pending intent must have
   the same source; otherwise a new intent is saved. *)
PLock(x) ==
  /\ pc[x] = "pLock"
  /\ IF pHead # pAcc
     THEN Refuse(x) /\ UNCHANGED dIntent
     ELSE IF dRecord = pAcc
          THEN Refuse(x) /\ UNCHANGED dIntent    \* already integrated: returns, leases dropped
          ELSE IF dIntent # {} /\ dIntent # pAcc
               THEN Refuse(x) /\ UNCHANGED dIntent
               ELSE dIntent' = pAcc /\ Go(x, "pMerge") /\ UNCHANGED <<lockP, lockD>>
  /\ UNCHANGED <<pHead, pRemote, pAcc, pOwn, cIntent, cChecked, cRecord, dLocal, dRemote, dRecord, accepted, kills, commits>>

\* Lines 1730-1785: fast-forward or merge into the default checkout.
PMerge(x) ==
  /\ pc[x] = "pMerge"
  /\ dLocal' = dLocal \cup dIntent /\ Go(x, "pPush")
  /\ UNCHANGED <<pHead, pRemote, pAcc, pOwn, cIntent, cChecked, cRecord, dIntent, dRemote, dRecord, accepted, lockP, lockD, kills, commits>>

\* Lines 1795-1825: the parent's validation in the default checkout, then the push.
PPush(x) ==
  /\ pc[x] = "pPush"
  /\ dRemote' = dLocal /\ Go(x, "pRecord")
  /\ UNCHANGED <<pHead, pRemote, pAcc, pOwn, cIntent, cChecked, cRecord, dIntent, dLocal, dRecord, accepted, lockP, lockD, kills, commits>>

PRecord(x) ==
  /\ pc[x] = "pRecord"
  /\ dRecord' = dIntent /\ dIntent' = {}
  /\ Refuse(x)
  /\ UNCHANGED <<pHead, pRemote, pAcc, pOwn, cIntent, cChecked, cRecord, dLocal, dRemote, accepted, kills, commits>>

----------------------------------------------------------------------------

\* A process dies; the OS drops its leases.
Kill(x) ==
  /\ pc[x] # "idle" /\ kills < MaxKills
  /\ ParentEdgeKills \/ pc[x] \notin {"pLock", "pMerge", "pPush", "pRecord"}
  /\ Refuse(x) /\ kills' = kills + 1
  /\ UNCHANGED <<pHead, pRemote, pAcc, pOwn, cIntent, cChecked, cRecord, dIntent, dLocal, dRemote, dRecord, accepted, commits>>

(* The parent's session commits in its checkout. run() cannot start while a
   mutation holds the parent's lease; nothing refuses it otherwise. *)
SessionCommits ==
  /\ Writer = "session" /\ commits < 1 /\ lockP = None
  /\ pHead' = pHead \cup {"m"} /\ commits' = commits + 1
  /\ UNCHANGED <<pRemote, pAcc, pOwn, cIntent, cChecked, cRecord, dIntent, dLocal, dRemote, dRecord, accepted, lockP, lockD, pc, kills>>

ChildSteps == CLock \/ CMerge \/ CMerged \/ CValidate \/ CPush \/ CRecord
EdgeSteps(x) == PLock(x) \/ PMerge(x) \/ PPush(x) \/ PRecord(x)

Next ==
  \/ CStart \/ ChildSteps \/ FStart \/ FEnter
  \/ \E x \in Procs : PStart(x) \/ EdgeSteps(x) \/ Kill(x)
  \/ SessionCommits

(* The child's owner runs the child's finish again after any failure; a
   running process keeps going. When it runs, the parent's owner also runs
   the parent's finish again, so a new parent commit is accepted. *)
Spec == Init /\ [][Next]_vars
        /\ SF_vars(CStart) /\ WF_vars(ChildSteps) /\ SF_vars(FStart) /\ WF_vars(FEnter)
        /\ \A x \in Procs : SF_vars(PStart(x)) /\ WF_vars(EdgeSteps(x))

----------------------------------------------------------------------------
(* Rules *)

\* The parent's own work in its acceptance is work that a parent finish accepted.
ParentAcceptsOnlyItsOwn == pAcc \ {"c"} \subseteq pOwn

\* The child's work enters the parent's acceptance only after the child's and
\* the parent's validation ran on it in the parent checkout.
ChildCheckedInParent == "c" \in pAcc => cChecked

\* The parent's acceptance commit is on the parent's remote topic.
AcceptedIsPushed == pAcc \subseteq pRemote

\* The child's integration record names work that the parent's acceptance holds.
ChildRecordInParent == cRecord \subseteq pAcc

\* The remote default branch receives only commits the parent accepted.
DefaultGetsAccepted == dRemote = {} \/ dRemote \in accepted

\* Running the child's finish again always brings the child's work to the default branch.
ChildReachesDefault == <>("c" \in dRemote)
=============================================================================
