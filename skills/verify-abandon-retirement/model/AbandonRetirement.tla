---- MODULE AbandonRetirement ----
\* retire --abandon in src/workspace_lifecycle/service.py: a task that will not be
\* integrated ends at its branch HEAD, and reclaim removes the checkout only while
\* the branch keeps that commit. Commits 0..MaxCommit form one line; commit m
\* contains commit n when m >= n. Branch NoBranch means the ref was deleted.
EXTENDS Naturals

CONSTANTS MaxCommit, NoBranch

VARIABLES
  phase,       \* "active", "requested", "retired", "reclaimed"
  branch,      \* topic branch tip, or NoBranch
  dirty,       \* the checkout holds data that no commit holds
  held,        \* the task has a hold
  intent,      \* a finish recorded an integration it has not pushed
  integrated,  \* the task's commit reached the remote default
  recorded,    \* the commit the abandonment recorded, or NoBranch
  pushedAfter, \* something was pushed after abandonment was requested
  heldAtAsk,   \* a hold existed when abandonment was requested
  moved,       \* [commit, clean] of the checkout that retirement moved
  lost         \* reclaim removed data that no kept ref holds

vars == <<phase, branch, dirty, held, intent, integrated, recorded, pushedAfter, heldAtAsk, moved, lost>>

Init ==
  /\ phase = "active" /\ branch = 0 /\ dirty = FALSE /\ held = FALSE
  /\ intent = FALSE /\ integrated = FALSE /\ recorded = NoBranch
  /\ pushedAfter = FALSE /\ heldAtAsk = FALSE /\ moved = <<NoBranch, TRUE>> /\ lost = FALSE

Checkout == phase \in {"active", "requested"}
Contains(tip, c) == tip # NoBranch /\ tip >= c

\* The user commits in the checkout; the branch follows HEAD.
UserCommits ==
  /\ Checkout /\ branch # NoBranch /\ branch < MaxCommit
  /\ branch' = branch + 1 /\ dirty' = FALSE
  /\ UNCHANGED <<phase, held, intent, integrated, recorded, pushedAfter, heldAtAsk, moved, lost>>

UserEdits ==
  /\ Checkout /\ dirty' = ~dirty
  /\ UNCHANGED <<phase, branch, held, intent, integrated, recorded, pushedAfter, heldAtAsk, moved, lost>>

Hold ==
  /\ Checkout /\ held' = ~held
  /\ UNCHANGED <<phase, branch, dirty, intent, integrated, recorded, pushedAfter, heldAtAsk, moved, lost>>

\* finish() commits nothing new here and records an integration intent. It is
\* refused while held and once a retire request carries an abandonment.
FinishStarts ==
  /\ phase = "active" /\ ~held /\ ~integrated /\ ~dirty /\ ~intent
  /\ intent' = TRUE
  /\ UNCHANGED <<phase, branch, dirty, held, integrated, recorded, pushedAfter, heldAtAsk, moved, lost>>

\* A pending intent can still be pushed, also by the settlement at finish entry.
FinishPushes ==
  /\ intent
  /\ intent' = FALSE /\ integrated' = TRUE
  /\ pushedAfter' = (pushedAfter \/ phase # "active")
  /\ UNCHANGED <<phase, branch, dirty, held, recorded, heldAtAsk, moved, lost>>

FinishWithdraws ==
  /\ intent /\ intent' = FALSE
  /\ UNCHANGED <<phase, branch, dirty, held, integrated, recorded, pushedAfter, heldAtAsk, moved, lost>>

\* retire --abandon saves the request with the branch HEAD.
AbandonRequested ==
  /\ phase = "active" /\ ~held /\ ~intent /\ ~integrated
  /\ phase' = "requested" /\ recorded' = branch /\ heldAtAsk' = held
  /\ UNCHANGED <<branch, dirty, held, intent, integrated, pushedAfter, moved, lost>>

\* retire replays the request: hold, branch HEAD and contents checks, then
\* git worktree move. verify-retire-move covers the window inside this step.
Retires ==
  /\ phase = "requested" /\ ~held
  /\ branch = recorded
  /\ ~dirty
  /\ phase' = "retired" /\ moved' = <<branch, ~dirty>>
  /\ UNCHANGED <<branch, dirty, held, intent, integrated, recorded, pushedAfter, heldAtAsk, lost>>

\* After retirement the user may delete or reset the branch with Git.
UserMovesBranch ==
  /\ phase \in {"retired", "reclaimed"}
  /\ \E tip \in (0..MaxCommit) \cup {NoBranch} : tip # branch /\ branch' = tip
  /\ UNCHANGED <<phase, dirty, held, intent, integrated, recorded, pushedAfter, heldAtAsk, moved, lost>>

\* reclaim with preservation evidence removes the recovery payload.
Reclaims ==
  /\ phase = "retired"
  /\ Contains(branch, recorded)
  /\ phase' = "reclaimed"
  /\ lost' = (lost \/ ~Contains(branch, moved[1]) \/ ~moved[2])
  /\ UNCHANGED <<branch, dirty, held, intent, integrated, recorded, pushedAfter, heldAtAsk, moved>>

Next ==
  \/ UserCommits \/ UserEdits \/ Hold \/ FinishStarts \/ FinishPushes \/ FinishWithdraws
  \/ AbandonRequested \/ Retires \/ UserMovesBranch \/ Reclaims

Spec == Init /\ [][Next]_vars

\* Abandonment never leads to a push or an integration.
NothingPushedAfterAbandonment == ~pushedAfter
AbandonedNeverIntegrated == phase # "active" => ~integrated
\* A held task is never abandoned until the hold is released.
NoAbandonmentWhileHeld == ~heldAtAsk
\* Retirement moves exactly the recorded commit, with no unsaved data.
RetiresRecordedCleanCommit == phase \in {"retired", "reclaimed"} => moved = <<recorded, TRUE>>
\* Reclaim removes only data that a kept ref still holds.
ReclaimLosesNothing == ~lost
====
