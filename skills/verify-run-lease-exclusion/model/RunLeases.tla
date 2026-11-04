---- MODULE RunLeases ----
\* Model of src/workspace_lifecycle/leases.py: run() supervisors, guard()
\* mutations and release(). One action is one lock call, one receipt write
\* or one process event in that file.
CONSTANTS Sessions, \* run() supervisors of one task
          Windows, \* TRUE: run() saves windows_job before Popen and the command
                  \* holds the job; FALSE (Linux): release() finds the command
                  \* by the use token in its environment
          Release \* "never"; "after-death": lease-release only while no supervisor
                  \* lives, as its recovery text asks; "any": at any time

None == <<"none">>
Out == <<"out">> \* guard(allow_use=False) from outside every session
Rel == <<"rel">> \* release(), as lease-release and the reclaim path in cli.py
In(s) == <<"in", s>> \* guard(allow_use=True) run by session s's own child

VARIABLES
  pc,        \* per session: the run() line it is at
  sup,       \* per session: supervisor process alive
  child,     \* per session: child command alive (it uses the checkout)
  receipt,   \* per session: "none", "bare" (no child or job recorded), "tracked"
  reg,       \* holder of <task>.register.lock
  mut,       \* holder of <task>.mutation.lock
  shared,    \* sessions holding <task>.lock shared
  excl,      \* holder of <task>.lock exclusive
  outPc, inPc, relPc, seen,
  relPick    \* release(): <<session, receipt as read>>

vars == <<pc, sup, child, receipt, reg, mut, shared, excl, outPc, inPc, relPc, seen, relPick>>

Init ==
  /\ pc = [s \in Sessions |-> "idle"]
  /\ sup = [s \in Sessions |-> FALSE]
  /\ child = [s \in Sessions |-> FALSE]
  /\ receipt = [s \in Sessions |-> "none"]
  /\ reg = None /\ mut = None /\ shared = {} /\ excl = None
  /\ outPc = "idle" /\ inPc = [s \in Sessions |-> "idle"] /\ relPc = "idle"
  /\ seen = {} /\ relPick = None

Receipts == {s \in Sessions : receipt[s] # "none"}

\* ---- run(): leases.py:434-531 ----

RunRegister(s) == \* with _lock(register.lock, blocking=True)
  /\ pc[s] = "idle" /\ reg = None
  /\ reg' = s /\ sup' = [sup EXCEPT ![s] = TRUE] /\ pc' = [pc EXCEPT ![s] = "probe"]
  /\ UNCHANGED <<child, receipt, mut, shared, excl, outPc, inPc, relPc, seen, relPick>>

RunProbe(s) == \* non-blocking mutation.lock probe; UseConflict when held
  /\ pc[s] = "probe" /\ sup[s]
  /\ IF mut = None
       THEN pc' = [pc EXCEPT ![s] = "share"] /\ reg' = reg /\ sup' = sup
       ELSE pc' = [pc EXCEPT ![s] = "idle"] /\ reg' = None /\ sup' = [sup EXCEPT ![s] = FALSE]
  /\ UNCHANGED <<child, receipt, mut, shared, excl, outPc, inPc, relPc, seen, relPick>>

RunShare(s) == \* use.enter_context(_lock(lock, shared=True))
  /\ pc[s] = "share" /\ sup[s]
  /\ IF excl = None
       THEN pc' = [pc EXCEPT ![s] = "save"] /\ shared' = shared \cup {s} /\ reg' = reg /\ sup' = sup
       ELSE pc' = [pc EXCEPT ![s] = "idle"] /\ shared' = shared /\ reg' = None /\ sup' = [sup EXCEPT ![s] = FALSE]
  /\ UNCHANGED <<child, receipt, mut, excl, outPc, inPc, relPc, seen, relPick>>

RunSaveReceipt(s) == \* _save(receipt, record) without child; register.lock released
  /\ pc[s] = "save" /\ sup[s]
  /\ receipt' = [receipt EXCEPT ![s] = "bare"] /\ reg' = None
  /\ pc' = [pc EXCEPT ![s] = IF Windows THEN "job" ELSE "spawn"]
  /\ UNCHANGED <<sup, child, mut, shared, excl, outPc, inPc, relPc, seen, relPick>>

RunSaveJob(s) == \* Windows: record['windows_job'] = job; _save(receipt, record)
  /\ pc[s] = "job" /\ sup[s]
  /\ receipt' = [receipt EXCEPT ![s] = "tracked"] /\ pc' = [pc EXCEPT ![s] = "spawn"]
  /\ UNCHANGED <<sup, child, reg, mut, shared, excl, outPc, inPc, relPc, seen, relPick>>

RunSpawn(s) == \* child = subprocess.Popen(argv, **options)
  /\ pc[s] = "spawn" /\ sup[s]
  /\ child' = [child EXCEPT ![s] = TRUE] /\ pc' = [pc EXCEPT ![s] = "record"]
  /\ UNCHANGED <<sup, receipt, reg, mut, shared, excl, outPc, inPc, relPc, seen, relPick>>

RunRecordChild(s) == \* record.update(child_pid, child_identity, process_group); _save
  /\ pc[s] = "record" /\ sup[s]
  /\ receipt' = [receipt EXCEPT ![s] = "tracked"] /\ pc' = [pc EXCEPT ![s] = "wait"]
  /\ UNCHANGED <<sup, child, reg, mut, shared, excl, outPc, inPc, relPc, seen, relPick>>

ChildExits(s) == \* the command and its descendants end; an inner mutation ends first
  /\ child[s] /\ inPc[s] = "idle"
  /\ child' = [child EXCEPT ![s] = FALSE]
  /\ UNCHANGED <<pc, sup, receipt, reg, mut, shared, excl, outPc, inPc, relPc, seen, relPick>>

RunComplete(s) == \* descendants gone: _remove(receipt), then the shared lock closes
  /\ pc[s] = "wait" /\ sup[s] /\ ~child[s]
  /\ receipt' = [receipt EXCEPT ![s] = "none"] /\ pc' = [pc EXCEPT ![s] = "unlock"]
  /\ UNCHANGED <<sup, child, reg, mut, shared, excl, outPc, inPc, relPc, seen, relPick>>

RunUnlock(s) ==
  /\ pc[s] = "unlock" /\ sup[s]
  /\ shared' = shared \ {s} /\ sup' = [sup EXCEPT ![s] = FALSE] /\ pc' = [pc EXCEPT ![s] = "idle"]
  /\ UNCHANGED <<child, receipt, reg, mut, excl, outPc, inPc, relPc, seen, relPick>>

SupervisorCrashes(s) == \* SIGKILL: the OS drops its locks, receipt and child remain
  /\ sup[s] /\ pc[s] \notin {"idle", "unlock"}
  /\ sup' = [sup EXCEPT ![s] = FALSE] /\ pc' = [pc EXCEPT ![s] = "crashed"]
  /\ shared' = shared \ {s} /\ reg' = IF reg = s THEN None ELSE reg
  /\ UNCHANGED <<child, receipt, mut, excl, outPc, inPc, relPc, seen, relPick>>

\* ---- guard(): leases.py:148-177 ----

\* _mutation: register.lock blocking, then mutation.lock non-blocking. A busy
\* mutation.lock is a UseConflict that changes nothing, so it has no action.
CanEnter == reg = None /\ mut = None

OwnUse(s) == Receipts = {s} /\ sup[s] \* _own_use: token, task, owner identity alive

\* ---- outside mutation ----

OutEnter ==
  /\ outPc = "idle" /\ CanEnter
  /\ mut' = Out /\ outPc' = "uses"
  /\ UNCHANGED <<pc, sup, child, receipt, reg, shared, excl, inPc, relPc, seen, relPick>>

OutReadUses == \* uses = _uses(lock)
  /\ outPc = "uses"
  /\ seen' = Receipts /\ outPc' = "excl"
  /\ UNCHANGED <<pc, sup, child, receipt, reg, mut, shared, excl, inPc, relPc, relPick>>

OutExclusive == \* _lock(lock) exclusive non-blocking, then `if uses: raise`
  /\ outPc = "excl"
  /\ IF shared = {} /\ excl = None /\ seen = {}
       THEN excl' = Out /\ outPc' = "mutating" /\ mut' = mut
       ELSE excl' = excl /\ outPc' = "idle" /\ mut' = None
  /\ seen' = {}
  /\ UNCHANGED <<pc, sup, child, receipt, reg, shared, inPc, relPc, relPick>>

OutDone ==
  /\ outPc = "mutating"
  /\ excl' = None /\ mut' = None /\ outPc' = "idle"
  /\ UNCHANGED <<pc, sup, child, receipt, reg, shared, inPc, relPc, seen, relPick>>

\* ---- mutation from inside session s (finish/hold/retire with allow_use) ----

InEnter(s) ==
  /\ child[s] /\ inPc[s] = "idle" /\ CanEnter
  /\ mut' = In(s) /\ inPc' = [inPc EXCEPT ![s] = "uses"]
  /\ UNCHANGED <<pc, sup, child, receipt, reg, shared, excl, outPc, relPc, seen, relPick>>

InDecide(s) == \* sole own use proceeds; otherwise the same exclusive path as Out
  /\ inPc[s] = "uses"
  /\ CASE OwnUse(s) ->
            inPc' = [inPc EXCEPT ![s] = "mutating"] /\ mut' = mut /\ excl' = excl
       [] shared = {} /\ excl = None /\ Receipts = {} ->
            inPc' = [inPc EXCEPT ![s] = "mutating"] /\ mut' = mut /\ excl' = In(s)
       [] OTHER ->
            inPc' = [inPc EXCEPT ![s] = "idle"] /\ mut' = None /\ excl' = excl
  /\ UNCHANGED <<pc, sup, child, receipt, reg, shared, outPc, relPc, seen, relPick>>

InDone(s) ==
  /\ inPc[s] = "mutating"
  /\ mut' = None /\ excl' = IF excl = In(s) THEN None ELSE excl
  /\ inPc' = [inPc EXCEPT ![s] = "idle"]
  /\ UNCHANGED <<pc, sup, child, receipt, reg, shared, outPc, relPc, seen, relPick>>

\* ---- release(): leases.py:536-564 ----

RelEnter ==
  /\ Release # "never"
  /\ Release = "after-death" => \A s \in Sessions : ~sup[s]
  /\ relPc = "idle" /\ CanEnter
  /\ mut' = Rel /\ relPc' = "uses"
  /\ UNCHANGED <<pc, sup, child, receipt, reg, shared, excl, outPc, inPc, seen, relPick>>

RelPick == \* matches[0]: the receipt of the token given
  /\ relPc = "uses"
  /\ IF Receipts = {}
       THEN relPc' = "idle" /\ mut' = None /\ relPick' = None
       ELSE \E s \in Receipts : relPick' = <<s, receipt[s]>> /\ relPc' = "check" /\ mut' = mut
  /\ UNCHANGED <<pc, sup, child, receipt, reg, shared, excl, outPc, inPc, seen>>

RelCheck == \* owner alive; then, from the receipt read again, the job (Windows)
            \* or any process with the use token (Linux)
  /\ relPc = "check"
  /\ LET s == relPick[1] IN
       IF sup[s] \/ (child[s] /\ (receipt[s] = "tracked" \/ ~Windows))
         THEN relPc' = "idle" /\ mut' = None /\ receipt' = receipt
         ELSE relPc' = "idle" /\ mut' = None /\ receipt' = [receipt EXCEPT ![s] = "none"]
  /\ relPick' = None
  /\ UNCHANGED <<pc, sup, child, reg, shared, excl, outPc, inPc, seen>>

\* ----

Next ==
  \/ \E s \in Sessions :
       \/ RunRegister(s) \/ RunProbe(s) \/ RunShare(s) \/ RunSaveReceipt(s) \/ RunSaveJob(s)
       \/ RunSpawn(s) \/ RunRecordChild(s) \/ ChildExits(s) \/ RunComplete(s) \/ RunUnlock(s)
       \/ SupervisorCrashes(s) \/ InEnter(s) \/ InDecide(s) \/ InDone(s)
  \/ OutEnter \/ OutReadUses \/ OutExclusive \/ OutDone
  \/ RelEnter \/ RelPick \/ RelCheck

\* While a mutation changes the task, no other session's command runs in it.
NoMutationUnderAnotherSession ==
  /\ outPc = "mutating" => \A s \in Sessions : ~child[s]
  /\ \A m \in Sessions : inPc[m] = "mutating" => \A s \in Sessions \ {m} : ~child[s]

TypeOK ==
  /\ receipt \in [Sessions -> {"none", "bare", "tracked"}]
  /\ shared \subseteq Sessions
====
