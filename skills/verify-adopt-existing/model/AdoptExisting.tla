---------------------------- MODULE AdoptExisting ----------------------------
(* adoption.py adopt_existing(): two requests adopt the same existing branch
   checkout as tasks "a" and "b". The body runs under the state lock; its
   effects are the saved intent with its snapshot, the Git branch binding and
   the saved task. A request can be killed at any step and retried with the
   same arguments, and a writer can change the checkout's dirty content. *)
EXTENDS Naturals

CONSTANTS
  Writer,     \* "none"; "contract": changes the checkout only while no adoption runs; "stray": at any time
  MaxKills

Tasks == {"a", "b"}
None == "none"
Contents == {"d0", "d1"}   \* the dirty content of the checkout

VARIABLES
  content,   \* the checkout's dirty content now
  intent,    \* intent[t]: the snapshot saved by t's adoption intent, or none
  binding,   \* branch.<branch>.workspaceTask: none or the task it names
  task,      \* task[t]: the baseline_dirty of task t, or none
  lock,      \* the request inside the state lock, or none
  pc,        \* pc[t]
  snap,      \* snap[t]: the capture taken at entry
  kills,
  writes

vars == <<content, intent, binding, task, lock, pc, snap, kills, writes>>

Init ==
  /\ content = "d0" /\ intent = [t \in Tasks |-> None] /\ binding = None
  /\ task = [t \in Tasks |-> None] /\ lock = None /\ pc = [t \in Tasks |-> "idle"]
  /\ snap = [t \in Tasks |-> None] /\ kills = 0 /\ writes = 0

Other(t) == CHOOSE u \in Tasks : u # t
Refuse(t) == pc' = [pc EXCEPT ![t] = "idle"] /\ lock' = None

\* The owner runs adopt-existing for t, again with the same arguments after a failure.
Start(t) ==
  /\ pc[t] = "idle" /\ task[t] = None /\ lock = None
  /\ pc' = [pc EXCEPT ![t] = "enter"] /\ lock' = t
  /\ UNCHANGED <<content, intent, binding, task, snap, kills, writes>>

\* Lines 225-259: refusals, _bindings, the first _capture, and the intent save.
Enter(t) ==
  /\ pc[t] = "enter"
  /\ IF \/ intent[Other(t)] # None                          \* another pending task owns this branch
        \/ binding # None /\ ~(intent[t] # None /\ binding = t)   \* _bindings(retry)
     THEN Refuse(t) /\ UNCHANGED <<intent, snap>>
     ELSE /\ snap' = [snap EXCEPT ![t] = content]
          /\ intent' = IF intent[t] = None THEN [intent EXCEPT ![t] = content] ELSE intent
          /\ pc' = [pc EXCEPT ![t] = "compare"] /\ UNCHANGED lock
  /\ UNCHANGED <<content, binding, task, kills, writes>>

\* Lines 260-261: the capture must match the intent's snapshot, twice.
Compare(t) ==
  /\ pc[t] = "compare"
  /\ IF snap[t] = intent[t] /\ content = intent[t]
     THEN pc' = [pc EXCEPT ![t] = "bind"] /\ UNCHANGED lock
     ELSE Refuse(t)
  /\ UNCHANGED <<content, intent, binding, task, snap, kills, writes>>

\* Lines 262-263: git config branch.<branch>.workspaceTask.
Bind(t) ==
  /\ pc[t] = "bind"
  /\ binding' = t /\ pc' = [pc EXCEPT ![t] = "recheck"]
  /\ UNCHANGED <<content, intent, task, lock, snap, kills, writes>>

\* Lines 264-266: the checkout must still match after binding.
Recheck(t) ==
  /\ pc[t] = "recheck"
  /\ IF content = intent[t] THEN pc' = [pc EXCEPT ![t] = "save"] /\ UNCHANGED lock
     ELSE Refuse(t)
  /\ UNCHANGED <<content, intent, binding, task, snap, kills, writes>>

\* Lines 267-281: save the task with the snapshot as baseline, drop the intent.
Save(t) ==
  /\ pc[t] = "save"
  /\ task' = [task EXCEPT ![t] = intent[t]] /\ intent' = [intent EXCEPT ![t] = None]
  /\ pc' = [pc EXCEPT ![t] = "idle"] /\ lock' = None
  /\ UNCHANGED <<content, binding, snap, kills, writes>>

\* The request dies; the OS drops its leases and the state lock.
Kill(t) ==
  /\ pc[t] # "idle" /\ kills < MaxKills
  /\ pc' = [pc EXCEPT ![t] = "idle"] /\ lock' = None /\ kills' = kills + 1
  /\ UNCHANGED <<content, intent, binding, task, snap, writes>>

WriterChanges ==
  /\ writes < 1
  /\ \/ Writer = "contract" /\ lock = None
     \/ Writer = "stray"
  /\ content' = IF content = "d0" THEN "d1" ELSE "d0"
  /\ writes' = writes + 1
  /\ UNCHANGED <<intent, binding, task, lock, pc, snap, kills>>

Done ==
  /\ lock = None /\ \A t \in Tasks : pc[t] = "idle"
  /\ UNCHANGED vars

Progress == \E t \in Tasks : Start(t) \/ Enter(t) \/ Compare(t) \/ Bind(t) \/ Recheck(t) \/ Save(t)

Next == Progress \/ (\E t \in Tasks : Kill(t)) \/ WriterChanges \/ Done

Steps(t) == Enter(t) \/ Compare(t) \/ Bind(t) \/ Recheck(t) \/ Save(t)

\* Each owner keeps retrying its own request, and a running request keeps going.
Spec == Init /\ [][Next]_vars /\ \A t \in Tasks : SF_vars(Start(t)) /\ WF_vars(Steps(t))

----------------------------------------------------------------------------
(* Rules *)

\* At most one task adopts the branch.
OneTaskPerBranch == ~(task["a"] # None /\ task["b"] # None)

\* The branch binding names a task that exists or whose adoption intent is pending.
NoOrphanBinding == binding # None => task[binding] # None \/ intent[binding] # None

\* A task is saved only while the checkout still holds the dirty content its
\* baseline records, so finish never treats newer content as preexisting.
CheckoutMatchesBeforeSave == \A t \in Tasks : pc[t] = "save" => content = intent[t]

\* Without a writer, retrying the same request always ends with an adopted task.
AdoptionCompletes == (Writer = "none") => <>(task["a"] # None \/ task["b"] # None)
=============================================================================
