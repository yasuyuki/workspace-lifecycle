----------------------------- MODULE RequiredChecks -----------------------------
(* Which remote checks finish --wait-checks waits for, and when it integrates,
   in src/workspace_lifecycle/service.py and checks.py.

   One task on one topic branch. Check names:
     "a" - a check every commit's workflow runs and the default branch declares
     "x" - a check another task adds to the default branch (its workflow job
           and its entry in .github/required-checks.json) after this task began
     "o" - a check this task adds itself (job and declaration on its topic)
   A check run can appear on a commit only for a job in that commit's workflow,
   and only if GitHub turned the push into a workflow run.

   Between runs of finish the owner does what the refusal says: it requires the
   declared checks with update-required-checks when finish says they are
   omitted; merges the remote default branch into the topic when finish says
   the checks cannot appear on the commit; starts the commit's workflow again
   when finish says no run exists; and runs the same finish again when the
   wait timed out on running checks. It may also merge or start the workflow
   at other times. *)
EXTENDS Naturals, FiniteSets

CONSTANTS Evolves,     \* subset of BOOLEAN: another task adds "x" to the default branch
          OwnDecl,     \* subset of BOOLEAN: this task's commits declare and run "o"
          MaxLost,     \* pushes whose workflow run GitHub never creates
          MaxCommits   \* commits on the topic (each merge of the default branch adds one)

ASSUME Evolves \subseteq BOOLEAN /\ OwnDecl \subseteq BOOLEAN

Checks == {"a", "x", "o"}

VARIABLES pc,         \* step of the running finish; "idle" between runs
          evolves, own,
          defDecl,    \* checks .github/required-checks.json declares on the remote default branch
          defJobs,    \* jobs of the default branch's workflow
          head,       \* the topic commit (a number)
          headJobs,   \* jobs of the workflow in the topic commit's tree
          headDecl,   \* checks the topic commit's tree declares
          required,   \* the task's required_checks
          pushed,     \* the commit of the task's acceptance or candidate; 0 when none
          runs,       \* check -> "none" | "pending" | "success" on the pushed commit
          lost,
          policy,     \* the default branch declaration the last policy check read
          refused,    \* "none", "omit", "absent", "norun" or "pending": what the last refusal said
          omitAtStart,\* the declaration already named checks the task omits when finish started
          restored,   \* this finish ran the plan's restore entries
          lateRefusal,\* a refusal known at the start came after the restore
          integrated,
          gateHeld    \* at integration, every required check succeeded on the commit

vars == <<pc, evolves, own, defDecl, defJobs, head, headJobs, headDecl, required, pushed, runs,
          lost, policy, refused, omitAtStart, restored, lateRefusal, integrated, gateHeld>>

Own == IF own THEN {"o"} ELSE {}

Init ==
  /\ pc = "idle"
  /\ evolves \in Evolves
  /\ own \in OwnDecl
  /\ defDecl = {"a"} /\ defJobs = {"a"}
  /\ head = 1
  /\ headJobs = {"a"} \cup Own /\ headDecl = {"a"} \cup Own
  \* begin: checks.select() takes the default branch declaration.
  /\ required = {"a"}
  /\ pushed = 0
  /\ runs = [c \in Checks |-> "none"]
  /\ lost = 0
  /\ policy = {}
  /\ refused = "none"
  /\ omitAtStart = FALSE
  /\ restored = FALSE
  /\ lateRefusal = FALSE
  /\ integrated = FALSE
  /\ gateHeld = TRUE

Succeeded == {c \in Checks : runs[c] = "success"}

\* checks.source(): the checks the commit's own declaration adds to the task's.
Added == headDecl \ required

Gate == required \cup Added

Refuse(reason) ==
  /\ refused' = reason
  /\ lateRefusal' = (lateRefusal \/ (reason = "omit" /\ omitAtStart /\ restored))
  /\ pc' = "idle"

\* The owner runs finish again.
Start ==
  /\ pc = "idle" /\ ~integrated /\ refused = "none"
  /\ pc' = "pre"
  /\ omitAtStart' = ~(defDecl \subseteq required)
  /\ restored' = FALSE
  /\ UNCHANGED <<evolves, own, defDecl, defJobs, head, headJobs, headDecl, required, pushed, runs,
                 lost, policy, refused, lateRefusal, integrated, gateHeld>>

\* finish(): a task whose acceptance or candidate is the current HEAD resumes at
\* _integrate(). Otherwise the early refusals run before any effect, ending with
\* checks.require() of the remote default declaration; then the plan,
\* validation and push.
Pre ==
  /\ pc = "pre"
  /\ IF pushed = head THEN pc' = "integrate" /\ UNCHANGED <<refused, lateRefusal>>
     ELSE IF ~(defDecl \subseteq required) THEN Refuse("omit")
     ELSE pc' = "resolve" /\ UNCHANGED <<refused, lateRefusal>>
  /\ UNCHANGED <<evolves, own, defDecl, defJobs, head, headJobs, headDecl, required, pushed, runs,
                 lost, policy, omitAtStart, restored, integrated, gateHeld>>

\* _resolve_files(): the plan's restore entries delete their files.
Resolve ==
  /\ pc = "resolve"
  /\ restored' = TRUE
  /\ pc' = "push"
  /\ UNCHANGED <<evolves, own, defDecl, defJobs, head, headJobs, headDecl, required, pushed, runs,
                 lost, policy, refused, omitAtStart, lateRefusal, integrated, gateHeld>>

\* finish(): _push() of the topic and the acceptance or candidate record.
\* GitHub starts the commit's workflow, or loses the push event.
Push ==
  /\ pc = "push"
  /\ pushed' = head
  /\ \/ /\ runs' = [c \in Checks |-> IF c \in headJobs THEN "pending" ELSE "none"]
        /\ UNCHANGED lost
     \/ /\ lost < MaxLost
        /\ runs' = [c \in Checks |-> "none"]
        /\ lost' = lost + 1
  /\ pc' = "integrate"
  /\ UNCHANGED <<evolves, own, defDecl, defJobs, head, headJobs, headDecl, required,
                 policy, refused, omitAtStart, restored, lateRefusal, integrated, gateHeld>>

\* _integrate(prepare=True): _check_policy() reads the remote default
\* declaration, checks.require() refuses checks the task omits, and
\* checks.verify() of the gated checks; until they succeed, the prepared
\* integration waits.
Integrate ==
  /\ pc = "integrate"
  /\ policy' = defDecl
  /\ IF ~(defDecl \subseteq required)
       THEN Refuse("omit")
       ELSE pc' = "wait" /\ UNCHANGED <<refused, lateRefusal>>
  /\ UNCHANGED <<evolves, own, defDecl, defJobs, head, headJobs, headDecl, required, pushed, runs,
                 lost, omitAtStart, restored, integrated, gateHeld>>

\* The commit's workflow runs (one check suite of the app) exist and all finished.
SuitesCompleted == (\E c \in headJobs : runs[c] # "none") /\ (\A c \in headJobs : runs[c] # "pending")

\* No workflow run of the commit exists.
NoRun == \A c \in Checks : runs[c] = "none"

\* _await_checks() -> checks.wait(): success once every gated check succeeded.
\* checks.watch(absent=True) ends at once when a missing check's app has only
\* finished suites ("cannot appear": merge the remote default branch). At the
\* timeout, missing checks without any run of their app name starting the
\* workflow again; otherwise the recovery is "keep waiting".
Wait ==
  /\ pc = "wait"
  /\ IF Gate \subseteq Succeeded
       THEN pc' = "final" /\ UNCHANGED <<refused, lateRefusal>>
     ELSE IF \E c \in Gate : runs[c] = "none" /\ SuitesCompleted
       THEN Refuse("absent")
     ELSE IF NoRun
       THEN Refuse("norun")
       ELSE Refuse("pending")
  /\ UNCHANGED <<evolves, own, defDecl, defJobs, head, headJobs, headDecl, required, pushed, runs,
                 lost, policy, omitAtStart, restored, integrated, gateHeld>>

\* _integrate(reuse=...): the push to the default branch and its record.
Final ==
  /\ pc = "final"
  /\ integrated' = TRUE
  /\ gateHeld' = ((required \cup policy \cup headDecl) \subseteq Succeeded)
  /\ pc' = "done"
  /\ UNCHANGED <<evolves, own, defDecl, defJobs, head, headJobs, headDecl, required, pushed, runs,
                 lost, policy, refused, omitAtStart, restored, lateRefusal>>

\* A workflow run finishes.
Ci ==
  /\ \E c \in Checks : runs[c] = "pending" /\ runs' = [runs EXCEPT ![c] = "success"]
  /\ UNCHANGED <<pc, evolves, own, defDecl, defJobs, head, headJobs, headDecl, required, pushed,
                 lost, policy, refused, omitAtStart, restored, lateRefusal, integrated, gateHeld>>

\* Another task adds check "x" to the default branch: its job and its declaration.
Evolve ==
  /\ evolves /\ "x" \notin defDecl
  /\ defDecl' = defDecl \cup {"x"} /\ defJobs' = defJobs \cup {"x"}
  /\ UNCHANGED <<pc, evolves, own, head, headJobs, headDecl, required, pushed, runs,
                 lost, policy, refused, omitAtStart, restored, lateRefusal, integrated, gateHeld>>

\* The owner does what the refusal says.
Recover ==
  /\ pc = "idle" /\ refused # "none"
  /\ refused' = "none"
  \* "required checks omit ...; require them with update-required-checks"
  /\ required' = IF refused = "omit" THEN required \cup defDecl ELSE required
  \* "merge the remote default branch into the task ... then run finish again"
  /\ IF refused = "absent"
       THEN head' = head + 1 /\ headJobs' = defJobs \cup Own /\ headDecl' = defDecl \cup Own
       ELSE UNCHANGED <<head, headJobs, headDecl>>
  \* "no workflow run exists for it, so start one for this commit"
  /\ runs' = IF refused = "norun"
               THEN [c \in Checks |-> IF c \in headJobs /\ runs[c] = "none" THEN "pending" ELSE runs[c]]
               ELSE runs
  \* "pending": "run the same finish again to keep waiting"
  /\ UNCHANGED <<pc, evolves, own, defDecl, defJobs, pushed,
                 lost, policy, omitAtStart, restored, lateRefusal, integrated, gateHeld>>

\* The owner merges the remote default branch into the topic on its own.
Sync ==
  /\ pc = "idle" /\ refused = "none" /\ ~integrated /\ head < MaxCommits
  /\ head' = head + 1
  /\ headJobs' = defJobs \cup Own /\ headDecl' = defDecl \cup Own
  /\ UNCHANGED <<pc, evolves, own, defDecl, defJobs, required, pushed, runs,
                 lost, policy, refused, omitAtStart, restored, lateRefusal, integrated, gateHeld>>

\* The owner starts the pushed commit's workflow again on its own, for example
\* by pushing it to another ref.
Retrigger ==
  /\ pc = "idle" /\ refused = "none" /\ pushed = head /\ ~integrated
  /\ \E c \in headJobs : runs[c] = "none"
  /\ runs' = [c \in Checks |-> IF c \in headJobs /\ runs[c] = "none" THEN "pending" ELSE runs[c]]
  /\ UNCHANGED <<pc, evolves, own, defDecl, defJobs, head, headJobs, headDecl, required, pushed,
                 lost, policy, refused, omitAtStart, restored, lateRefusal, integrated, gateHeld>>

Done == integrated /\ UNCHANGED vars

FinishSteps == Start \/ Pre \/ Resolve \/ Push \/ Integrate \/ Wait \/ Final

Next == FinishSteps \/ Ci \/ Evolve \/ Recover \/ Sync \/ Retrigger \/ Done

\* The owner keeps running finish and following its refusals; CI finishes the
\* runs it started; nothing else makes the owner merge or start a workflow again.
Spec == Init /\ [][Next]_vars /\ WF_vars(FinishSteps) /\ WF_vars(Recover) /\ WF_vars(Ci)

------------------------------------------------------------------------------
\* Rules.

\* Integration happens only after every check the task requires, the default
\* branch declares and the commit declares succeeded on that commit.
GateHolds == gateHeld

\* A refusal finish could make from what it knew at its start comes before the
\* plan's restore deletes files.
NoEffectBeforeKnownRefusal == ~lateRefusal

\* Running finish again and doing what each refusal says integrates the task.
FinishCompletes == <>integrated
==============================================================================
