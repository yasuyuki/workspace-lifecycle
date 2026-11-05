---------------------------- MODULE FinishIntegration ----------------------------
(* The finish and integration steps of service.py: finish(), _integrate(),
   _await_checks() and _withdraw(), for one task whose integration target is
   the remote default branch. One action is one saved state change, one Git
   effect or one remote event. Another task ("o") integrates into the same
   default checkout, and the finish process can be killed at any step. *)
EXTENDS Naturals

CONSTANTS
  Gated,  \* the task requires remote checks (_gated_checks is not empty)
  Kills   \* the finish process can be killed (SIGKILL, closed terminal)

TaskCommits == {"c1", "c2"}   \* c1: the task's work; c2: a fix the owner commits later
None == "none"
NoIntent == [kind |-> None, src |-> None, before |-> {}, deferred |-> FALSE, checks |-> FALSE]
NoRecord == [kind |-> None, commit |-> None]

VARIABLES
  head,        \* the task checkout HEAD
  fixed,       \* the owner has committed c2
  valid,       \* valid[c]: the task validation passes in the task checkout at c
  validT,      \* validT[c]: the task validation passes in the default checkout with c merged
  ci,          \* ci[c]: the required checks on c: none, pending, success, failure
  topic,       \* the remote topic branch tip
  rec,         \* task['candidate'] or task['acceptance']
  integrated,  \* task['integrated']['source']
  intent,      \* state['intents'][task]: a finish or merge intent
  tLocal,      \* commits the default branch checkout contains
  tRemote,     \* commits the remote default branch contains
  otherDone,   \* the other task has integrated
  pc,          \* where the finish process is
  mode,        \* how the owner ran finish: plain, wait (--wait-checks), revise (--revise-plan-head)
  prep,        \* the running _integrate was called with prepare=True
  gate         \* the running _integrate read every check as succeeded (checks.verify)

vars == <<head, fixed, valid, validT, ci, topic, rec, integrated, intent,
          tLocal, tRemote, otherDone, pc, mode, prep, gate>>
task == <<head, fixed, valid, validT>>

Candidate == Gated /\ integrated # head
Deferred == intent.kind = "merge" /\ intent.deferred /\ ~intent.checks
\* Steps that hold the task and @default leases (_edge_leases).
EdgeLeased == {"integrate", "lock", "merge", "validateTarget", "gate", "accept",
               "pushTarget", "record2", "withdraw", "withdrawPop"}

Init ==
  /\ head = "c1" /\ fixed = FALSE
  /\ valid \in [TaskCommits -> BOOLEAN] /\ validT \in [TaskCommits -> BOOLEAN]
  /\ ci = [c \in TaskCommits |-> None]
  /\ topic = None /\ rec = NoRecord /\ integrated = None /\ intent = NoIntent
  /\ tLocal = {"base"} /\ tRemote = {"base"} /\ otherDone = FALSE
  /\ pc = "idle" /\ mode = "plain" /\ prep = FALSE /\ gate = FALSE

----------------------------------------------------------------------------
(* finish(): the locked block before _integrate *)

\* The owner runs finish, with or without --wait-checks or --revise-plan-head.
Start(m) ==
  /\ pc = "idle" /\ (integrated # head \/ intent.kind # None)
  /\ pc' = "enter" /\ mode' = m /\ prep' = (m = "wait")
  /\ UNCHANGED <<task, ci, topic, rec, integrated, intent, tLocal, tRemote, otherDone, gate>>

\* finish() lines 990-1074: resume a pending merge or accepted record, or open a finish intent.
FinEnter ==
  /\ pc = "enter"
  /\ CASE intent.kind = "merge" ->
            \* `intent['kind'] != 'finish'`: the saved record must be HEAD
            /\ pc' = IF mode # "revise" /\ rec.commit = head THEN "integrate" ELSE "idle"
            /\ UNCHANGED intent
       [] intent.kind = "finish" ->
            IF head = intent.src THEN pc' = "validate" /\ UNCHANGED intent
            ELSE IF mode = "revise"
                 THEN intent' = [intent EXCEPT !.src = head] /\ pc' = "validate"
                 ELSE pc' = "idle" /\ UNCHANGED intent  \* HEAD changed outside this finish intent
       [] OTHER ->
            IF mode # "revise" /\ rec.kind # None /\ rec.commit = head /\ topic = head
            THEN pc' = "integrate" /\ UNCHANGED intent  \* _acceptance_holds
            ELSE IF mode = "revise" THEN pc' = "idle" /\ UNCHANGED intent
            ELSE intent' = [NoIntent EXCEPT !.kind = "finish", !.src = head] /\ pc' = "validate"
  /\ UNCHANGED <<task, ci, topic, rec, integrated, tLocal, tRemote, otherDone, mode, prep, gate>>

\* finish() line 1152: a candidate skips the task checkout validation.
FinValidate ==
  /\ pc = "validate"
  /\ pc' = IF Candidate \/ valid[head] THEN "push" ELSE "idle"
  /\ UNCHANGED <<task, ci, topic, rec, integrated, intent, tLocal, tRemote, otherDone, mode, prep, gate>>

\* finish() line 1167: _push of the topic; CI starts on the pushed commit.
FinPush ==
  /\ pc = "push"
  /\ topic' = head
  /\ ci' = IF Gated /\ ci[head] = None THEN [ci EXCEPT ![head] = "pending"] ELSE ci
  /\ pc' = "record"
  /\ UNCHANGED <<task, rec, integrated, intent, tLocal, tRemote, otherDone, mode, prep, gate>>

\* finish() lines 1171-1188: save the candidate or acceptance and drop the finish intent.
FinRecord ==
  /\ pc = "record"
  /\ rec' = [kind |-> IF Candidate THEN "candidate" ELSE "acceptance", commit |-> head]
  /\ intent' = NoIntent
  /\ pc' = "integrate"
  /\ UNCHANGED <<task, ci, topic, integrated, tLocal, tRemote, otherDone, mode, prep, gate>>

----------------------------------------------------------------------------
(* _integrate() *)

\* Lines 1605-1633: read the state, then checks.verify outside the state lock.
IntVerify ==
  /\ pc = "integrate"
  /\ LET src == rec.commit
         settled == integrated = src \/ intent.kind = "merge"
         g == ci[src] = "success"
     IN IF Gated /\ (~settled \/ Deferred)
        THEN /\ gate' = g
             /\ pc' = IF g \/ prep THEN "lock" ELSE IF Deferred THEN "withdraw" ELSE "idle"
        ELSE gate' = FALSE /\ pc' = "lock"
  /\ UNCHANGED <<task, ci, topic, rec, integrated, intent, tLocal, tRemote, otherDone, mode, prep>>

\* Lines 1634-1726: identity and proof checks, synchronize the target with the
\* remote, and save the merge intent.
IntLock ==
  /\ pc = "lock"
  /\ LET src == rec.commit
         proof == (intent.kind = "merge" /\ intent.checks) \/ gate
         synced == tLocal \cup tRemote
     IN CASE integrated = src -> pc' = "idle" /\ UNCHANGED <<intent, tLocal>>
          [] intent.kind = "merge" /\ intent.src # src -> pc' = "idle" /\ UNCHANGED <<intent, tLocal>>
          [] Gated /\ (proof \/ ~prep) /\ ~proof -> pc' = "idle" /\ UNCHANGED <<intent, tLocal>>
          [] intent.kind = "merge" -> pc' = "merge" /\ UNCHANGED <<intent, tLocal>>
          [] OTHER ->
               /\ tLocal' = synced
               /\ intent' = [kind |-> "merge", src |-> src, before |-> synced,
                             deferred |-> Gated /\ ~gate, checks |-> Gated /\ gate]
               /\ pc' = "merge"
  /\ UNCHANGED <<task, ci, topic, rec, integrated, tRemote, otherDone, mode, prep, gate>>

\* Lines 1734-1784: fast-forward or merge the source into the default checkout.
IntMerge ==
  /\ pc = "merge"
  /\ tLocal' = tLocal \cup {intent.src}
  /\ pc' = "validateTarget"
  /\ UNCHANGED <<task, ci, topic, rec, integrated, intent, tRemote, otherDone, mode, prep, gate>>

\* Lines 1800-1813: validate in the target; a failure withdraws a deferred merge.
IntValidate ==
  /\ pc = "validateTarget"
  /\ pc' = IF validT[intent.src] THEN "gate" ELSE "withdraw"
  /\ UNCHANGED <<task, ci, topic, rec, integrated, intent, tLocal, tRemote, otherDone, mode, prep, gate>>

\* Lines 1817-1824: without check evidence return the prepared integration.
IntGate ==
  /\ pc = "gate"
  /\ IF Gated /\ ~intent.checks
     THEN IF ~gate THEN pc' = "await" /\ UNCHANGED intent
          ELSE intent' = [intent EXCEPT !.checks = TRUE] /\ pc' = "accept"
     ELSE pc' = "accept" /\ UNCHANGED intent
  /\ UNCHANGED <<task, ci, topic, rec, integrated, tLocal, tRemote, otherDone, mode, prep, gate>>

\* Lines 1825-1829: a candidate becomes the acceptance.
IntAccept ==
  /\ pc = "accept"
  /\ rec' = [rec EXCEPT !.kind = "acceptance"]
  /\ pc' = "pushTarget"
  /\ UNCHANGED <<task, ci, topic, integrated, intent, tLocal, tRemote, otherDone, mode, prep, gate>>

\* Line 1830: _push of the default checkout.
IntPush ==
  /\ pc = "pushTarget"
  /\ tRemote' = tLocal
  /\ pc' = "record2"
  /\ UNCHANGED <<task, ci, topic, rec, integrated, intent, tLocal, otherDone, mode, prep, gate>>

\* Lines 1831-1842: save the integration and drop the merge intent.
IntRecord ==
  /\ pc = "record2"
  /\ integrated' = intent.src /\ intent' = NoIntent
  /\ pc' = "idle"
  /\ UNCHANGED <<task, ci, topic, rec, tLocal, tRemote, otherDone, mode, prep, gate>>

----------------------------------------------------------------------------
(* finish() lines 1193-1204: wait for the checks, then push or withdraw *)

\* _await_checks holds no lease or lock. A pending check here is the wait timing out.
Await ==
  /\ pc = "await"
  /\ IF ci[rec.commit] = "success"
     THEN pc' = "integrate" /\ prep' = FALSE   \* _integrate(reuse=prepared)
     ELSE pc' = "withdraw" /\ UNCHANGED prep    \* _withdraw_prepared
  /\ UNCHANGED <<task, ci, topic, rec, integrated, intent, tLocal, tRemote, otherDone, mode, gate>>

\* _withdraw() lines 1560-1584: return the default checkout to its HEAD before the merge.
Withdraw ==
  /\ pc = "withdraw"
  /\ IF ~Deferred THEN pc' = "idle" /\ UNCHANGED tLocal
     ELSE IF tLocal = intent.before THEN pc' = "withdrawPop" /\ UNCHANGED tLocal
     ELSE IF tLocal \subseteq tRemote THEN pc' = "idle" /\ UNCHANGED tLocal  \* may already be on the remote
     ELSE tLocal' = intent.before /\ pc' = "withdrawPop"                    \* git checkout -B
  /\ UNCHANGED <<task, ci, topic, rec, integrated, intent, tRemote, otherDone, mode, prep, gate>>

\* _withdraw() line 1587: drop the merge intent.
WithdrawPop ==
  /\ pc = "withdrawPop"
  /\ intent' = NoIntent /\ pc' = "idle"
  /\ UNCHANGED <<task, ci, topic, rec, integrated, tLocal, tRemote, otherDone, mode, prep, gate>>

----------------------------------------------------------------------------
(* Everything else *)

\* The finish process dies; the OS drops its leases and locks, the state file stays.
Kill ==
  /\ Kills /\ pc # "idle"
  /\ pc' = "idle"
  /\ UNCHANGED <<task, ci, topic, rec, integrated, intent, tLocal, tRemote, otherDone, mode, prep, gate>>

\* The owner commits a fix while no finish runs.
OwnerFix ==
  /\ pc = "idle" /\ ~fixed /\ integrated = None
  /\ head' = "c2" /\ fixed' = TRUE
  /\ UNCHANGED <<valid, validT, ci, topic, rec, integrated, intent, tLocal, tRemote, otherDone, pc, mode, prep, gate>>

CiCompletes ==
  \E c \in TaskCommits, r \in {"success", "failure"} :
    /\ ci[c] = "pending" /\ ci' = [ci EXCEPT ![c] = r]
    /\ UNCHANGED <<task, topic, rec, integrated, intent, tLocal, tRemote, otherDone, pc, mode, prep, gate>>

CiRerun ==
  \E c \in TaskCommits :
    /\ ci[c] = "failure" /\ ci' = [ci EXCEPT ![c] = "pending"]
    /\ UNCHANGED <<task, topic, rec, integrated, intent, tLocal, tRemote, otherDone, pc, mode, prep, gate>>

\* Another task's _integrate into the same default checkout: it takes the
\* @default lease, refuses while a merge intent targets the branch
\* (_refuse_pending_merge), synchronizes, merges its own commit and pushes.
OtherIntegrates ==
  /\ ~otherDone /\ pc \notin EdgeLeased /\ intent.kind # "merge"
  /\ LET t == tLocal \cup tRemote \cup {"o"} IN tLocal' = t /\ tRemote' = t
  /\ otherDone' = TRUE
  /\ UNCHANGED <<task, ci, topic, rec, integrated, intent, pc, mode, prep, gate>>

Done ==
  /\ pc = "idle" /\ integrated = head /\ intent.kind = None
  /\ UNCHANGED vars

Next ==
  \/ \E m \in {"plain", "wait", "revise"} : Start(m)
  \/ FinEnter \/ FinValidate \/ FinPush \/ FinRecord
  \/ IntVerify \/ IntLock \/ IntMerge \/ IntValidate \/ IntGate \/ IntAccept \/ IntPush \/ IntRecord
  \/ Await \/ Withdraw \/ WithdrawPop
  \/ Kill \/ OwnerFix \/ CiCompletes \/ CiRerun \/ OtherIntegrates \/ Done

Spec == Init /\ [][Next]_vars

----------------------------------------------------------------------------
(* Rules *)

\* Only work that passed its checks reaches the remote default branch: the
\* validation in the default checkout, and the required checks on the exact
\* commit (gated) or the validation in the task checkout (not gated).
OnlyCheckedWorkReachesDefault ==
  \A c \in TaskCommits :
    c \in tRemote => validT[c] /\ (IF Gated THEN ci[c] = "success" ELSE valid[c])

\* The default checkout never keeps an unpushed task commit that no pending
\* merge intent records; the next integration would push it unchecked.
NoForgottenPreparedMerge ==
  \A c \in TaskCommits :
    (c \in tLocal /\ c \notin tRemote) => intent.kind = "merge" /\ intent.src = c

\* A recorded integration is on the remote default branch.
IntegratedIsPushed == integrated # None => integrated \in tRemote

\* A prepared integration never outlives the task HEAD it was prepared for, so
\* after a fix commit finish can start again.
FixCanFinish == Deferred => head = intent.src
=============================================================================
