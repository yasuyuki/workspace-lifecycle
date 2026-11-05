----------------------------- MODULE RetireMove -----------------------------
(* service.py retire(): one accepted, integrated task checkout is moved to its
   recovery path, its Git admin entry is archived, its branch binding is unset
   and the retired receipt with the payload manifest is saved. The task's own
   session can run commands in the checkout through `run` and ask for the
   retirement from inside (finish --users-released). retire can be killed at
   any step and is retried with `retire --pending`. *)
EXTENDS Naturals

CONSTANTS
  Writer,        \* "none"; "contract": the session writes only while its run is active; "stray": at any time
  MaxWrites,     \* changes by the session to the checkout's content or HEAD
  MaxKills,
  KillInGitMove, \* TRUE: a kill can fall between git worktree move's rename and its gitdir write
  PathReuse      \* TRUE: another task can begin a checkout at the freed original path

None == "none"

VARIABLES
  payload,   \* where the checkout directory is: "active" (original path) or "recovery"
  gitdir,    \* the path the admin entry's gitdir file names: "active" or "recovery"
  admin,     \* the Git admin entry: "active" (.git/worktrees/<name>) or "archive"
  content,   \* the checkout's content: "clean" (tracked files only) or "dirty"
  head,      \* the task branch: "accepted" commit or "moved" by a later commit
  binding,   \* branch.<branch>.workspaceTask still names the task
  other,     \* another task's checkout is registered at the original path
  use,       \* the session's run in the checkout: "none" or "running"
  request,   \* the saved retirement request phase, or none
  receipt,   \* the content manifest in the saved retired receipt, or none
  lock,      \* a retire process holds the task lease
  pc,        \* that process's next step
  kills,
  writes

vars == <<payload, gitdir, admin, content, head, binding, other, use, request, receipt, lock, pc, kills, writes>>

Init ==
  /\ payload = "active" /\ gitdir = "active" /\ admin = "active"
  /\ content = "clean" /\ head = "accepted" /\ binding = TRUE /\ other = FALSE
  /\ use = "none" /\ request = None /\ receipt = None
  /\ lock = FALSE /\ pc = "idle" /\ kills = 0 /\ writes = 0

Refuse == pc' = "idle" /\ lock' = FALSE

(* Lines 2040-2073 for a new request, with the checks of 2001-2030. finish
   --users-released inside the worktree calls retire(request_only=True) under
   guard(allow_use=True): the session's own run may be active. It saves the
   request and returns before any move. *)
AskInside ==
  /\ use = "running" /\ request = None /\ receipt = None /\ ~lock
  /\ head = "accepted"
  /\ request' = "requested"
  /\ UNCHANGED <<payload, gitdir, admin, content, head, binding, other, use, receipt, lock, pc, kills, writes>>

(* retire from outside, or retire --pending: _lease(repo, task) without
   allow_use refuses while any run of the task is active (leases.py guard). *)
Start ==
  /\ pc = "idle" /\ ~lock /\ receipt = None
  /\ use = "none"
  /\ lock' = TRUE /\ pc' = "enter"
  /\ UNCHANGED <<payload, gitdir, admin, content, head, binding, other, use, request, receipt, kills, writes>>

(* Lines 2001-2083: accepted result, integration and branch HEAD checks; a new
   request is saved with phase "requested". *)
Enter ==
  /\ pc = "enter"
  /\ IF head # "accepted"
     THEN Refuse /\ UNCHANGED request
     ELSE /\ request' = IF request = None THEN "requested" ELSE request
          /\ pc' = IF payload = "recovery" THEN "held" ELSE "check"
          /\ UNCHANGED lock
  /\ UNCHANGED <<payload, gitdir, admin, content, head, binding, other, use, receipt, kills, writes>>

(* Lines 2100-2114: the checkout is still the registered worktree of the
   branch at the accepted HEAD; in phase "requested" _retirement_contents()
   refuses any nontracked data, then phase "moving" is saved. *)
Check ==
  /\ pc = "check"
  /\ IF ~(admin = "active" /\ gitdir = "active" /\ head = "accepted")
     THEN Refuse /\ UNCHANGED request
     ELSE IF request = "requested" /\ content # "clean"
          THEN Refuse /\ UNCHANGED request
          ELSE /\ request' = "moving"
               /\ pc' = "rename" /\ UNCHANGED lock
  /\ UNCHANGED <<payload, gitdir, admin, content, head, binding, other, use, receipt, kills, writes>>

(* Line 2115, git worktree move: Git renames the directory, then writes the
   new path into the admin entry's gitdir file. *)
Rename ==
  /\ pc = "rename"
  /\ payload' = "recovery" /\ pc' = "pointer"
  /\ UNCHANGED <<gitdir, admin, content, head, binding, other, use, request, receipt, lock, kills, writes>>

Pointer ==
  /\ pc = "pointer"
  /\ gitdir' = "recovery" /\ pc' = "held"
  /\ UNCHANGED <<payload, admin, content, head, binding, other, use, request, receipt, lock, kills, writes>>

(* Lines 2116-2129: with the admin entry still active, the recovery path must
   be its registered worktree at the accepted HEAD; phases "recovery-held"
   and "archiving-admin" are saved. *)
Held ==
  /\ pc = "held"
  /\ IF admin = "archive"
     THEN pc' = "unbind" /\ UNCHANGED <<request, lock>>
     ELSE IF gitdir = "recovery" /\ head = "accepted"
          THEN request' = "archiving-admin" /\ pc' = "archive" /\ UNCHANGED lock
          ELSE Refuse /\ UNCHANGED request
  /\ UNCHANGED <<payload, gitdir, admin, content, head, binding, other, use, receipt, kills, writes>>

\* Line 2130: admin.rename(archive).
Archive ==
  /\ pc = "archive"
  /\ admin' = "archive" /\ pc' = "unbind"
  /\ UNCHANGED <<payload, gitdir, content, head, binding, other, use, request, receipt, lock, kills, writes>>

(* Lines 2133-2145: the archived gitdir names the recovery path, neither path
   is an active worktree, the branch is still at the accepted commit; the
   branch binding is unset. *)
Unbind ==
  /\ pc = "unbind"
  /\ IF gitdir = "recovery" /\ admin = "archive" /\ ~other /\ head = "accepted"
     THEN binding' = FALSE /\ pc' = "receipt" /\ UNCHANGED lock
     ELSE Refuse /\ UNCHANGED binding
  /\ UNCHANGED <<payload, gitdir, admin, content, head, other, use, request, receipt, kills, writes>>

(* Lines 2146-2171: the manifest of the recovery payload goes into the
   retired receipt, the task and its request are removed, one persist(). *)
Receipt ==
  /\ pc = "receipt"
  /\ receipt' = content /\ request' = None
  /\ pc' = "idle" /\ lock' = FALSE
  /\ UNCHANGED <<payload, gitdir, admin, content, head, binding, other, use, kills, writes>>

\* The retire process dies; the OS drops its lease.
Kill ==
  /\ pc # "idle" /\ kills < MaxKills
  /\ KillInGitMove \/ pc # "pointer"
  /\ pc' = "idle" /\ lock' = FALSE /\ kills' = kills + 1
  /\ UNCHANGED <<payload, gitdir, admin, content, head, binding, other, use, request, receipt, writes>>

(* leases.py run(): the use lock is shared, so a run cannot start while retire
   holds the task lease; before_run() refuses a task with a retirement
   request (lines 2441-2460), and check_run_identity() needs the checkout at
   its original path. *)
RunStarts ==
  /\ use = "none" /\ ~lock /\ request = None /\ receipt = None /\ payload = "active"
  /\ use' = "running"
  /\ UNCHANGED <<payload, gitdir, admin, content, head, binding, other, request, receipt, lock, pc, kills, writes>>

RunEnds ==
  /\ use = "running"
  /\ use' = "none"
  /\ UNCHANGED <<payload, gitdir, admin, content, head, binding, other, request, receipt, lock, pc, kills, writes>>

\* The session writes or removes untracked data, or commits on the task branch.
SessionWrites ==
  /\ writes < MaxWrites /\ receipt = None
  /\ \/ Writer = "contract" /\ use = "running"
     \/ Writer = "stray"
  /\ \/ /\ content' = IF content = "clean" THEN "dirty" ELSE "clean"
        /\ UNCHANGED head
     \/ /\ head' = IF head = "accepted" THEN "moved" ELSE "accepted"
        /\ UNCHANGED content
  /\ writes' = writes + 1
  /\ UNCHANGED <<payload, gitdir, admin, binding, other, use, request, receipt, lock, pc, kills>>

\* begin of another task with --worktree at the original path, once it is free.
OtherBegins ==
  /\ PathReuse /\ ~other /\ payload = "recovery"
  /\ other' = TRUE
  /\ UNCHANGED <<payload, gitdir, admin, content, head, binding, use, request, receipt, lock, pc, kills, writes>>

Done ==
  /\ receipt # None /\ pc = "idle" /\ use = "none"
  /\ UNCHANGED vars

Steps == Enter \/ Check \/ Rename \/ Pointer \/ Held \/ Archive \/ Unbind \/ Receipt

Next ==
  \/ AskInside \/ Start \/ Steps \/ Kill
  \/ RunStarts \/ RunEnds \/ SessionWrites \/ OtherBegins \/ Done

\* The owner keeps retrying retire --pending, a running retire keeps going, and a run ends.
Spec == Init /\ [][Next]_vars /\ SF_vars(Start) /\ WF_vars(Steps) /\ WF_vars(RunEnds)

----------------------------------------------------------------------------
(* Rules *)

\* A checkout is never moved while a run of its task is active in it.
NoMoveInUse == ~(payload = "recovery" /\ use = "running")

\* The retired manifest holds only tracked content, so reclaim never deletes
\* data that the task did not own.
NoUnownedDataRetired == receipt # None => receipt = "clean"

\* Git's admin entry names the checkout where it is, except inside git worktree move.
AdminFindsCheckout == pc # "pointer" => gitdir = payload

\* The receipt is saved only once the checkout left the active worktree list,
\* its admin entry is archived, its branch is at the accepted commit and unbound.
ReceiptOnlyWhenRetired ==
  receipt # None =>
    /\ payload = "recovery" /\ gitdir = "recovery" /\ admin = "archive"
    /\ head = "accepted" /\ ~binding

\* Once retirement is requested, retire --pending always completes it.
RetireCompletes == (request # None) ~> (receipt # None)
=============================================================================
