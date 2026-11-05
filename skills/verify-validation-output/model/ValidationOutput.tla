---------------------------- MODULE ValidationOutput ----------------------------
(* The dirt checks of finish() and _integrate() in
   src/workspace_lifecycle/service.py, and the files a validator leaves behind.

   One task with one owned source change. Its checkout can also hold:
     "valout"  - ignored output its validator writes (a __pycache__/*.pyc)
     "manual"  - ignored output of a test the agent ran by hand in the checkout
     "foreign" - private data another session of the task writes while finish runs
   The integration target (the default branch checkout, or the parent task's
   checkout) can hold "valout" when the validator runs there.

   The task can declare its validator outputs. After each validation run,
   _run_validation() removes the declared outputs that run created.

   Between runs of finish the agent does what a refusal names: it deletes each
   named reproducible file, archives each named private file, and declares the
   outputs a refusal says the validation created. It may also run a test by
   hand, or replace the validator with one that writes nothing. *)
EXTENDS Naturals, FiniteSets

CONSTANTS Targets,     \* subset of {"default", "parent"}: what the task integrates into
          Gated,       \* subset of BOOLEAN: whether required checks gate the task
          Declared,    \* subset of BOOLEAN: whether begin declared the validator outputs
          TaskWrites,  \* the registered validator writes ignored output where it runs
          MaxManual,   \* tests the agent runs by hand in the task checkout
          MaxForeign   \* private files another session writes during a finish

ASSUME Targets \subseteq {"default", "parent"} /\ Gated \subseteq BOOLEAN /\ Declared \subseteq BOOLEAN

Reproducible == {"valout", "manual"}

VARIABLES pc,        \* step of the running finish; "idle" between runs
          kind,      \* "default" or "parent"
          gated,     \* required checks gate the task, so it is a candidate
          declared,  \* validation_outputs names the validator's output
          task,      \* dirty paths in the task checkout
          target,    \* ignored paths in the integration target checkout
          archive,   \* private paths the plan archives
          writes,    \* the current validator writes ignored output
          made,      \* paths the validation of this run created
          cleaned,   \* paths _run_validation() removed, over all runs
          refused,   \* paths the last refusal named
          pending,   \* a merge intent stays after an integration refusal
          accepted,  \* the task acceptance (or candidate record) is saved
          dirtAtAccept,
          integrated,
          manual, foreign

vars == <<pc, kind, gated, declared, task, target, archive, writes, made, cleaned, refused,
          pending, accepted, dirtAtAccept, integrated, manual, foreign>>

Init ==
  /\ pc = "idle"
  /\ kind \in Targets
  /\ gated \in Gated
  /\ declared \in Declared
  /\ task = {"owned"}
  /\ target = {}
  /\ archive = {}
  /\ writes = TaskWrites
  /\ made = {}
  /\ cleaned = {}
  /\ refused = {}
  /\ pending = FALSE
  /\ accepted = FALSE
  /\ dirtAtAccept = {}
  /\ integrated = FALSE
  /\ manual = 0
  /\ foreign = 0

\* _target_snapshot(): a default branch checkout's ignored data is not dirt.
TargetDirt == IF kind = "parent" THEN target ELSE {}

\* The paths a cleanup step may remove: declared outputs this run created.
Sweep(paths) == IF declared THEN paths \cap made ELSE {}

Refuse(paths) ==
  /\ refused' = paths
  /\ pc' = "idle"

\* The agent runs finish again.
Start ==
  /\ pc = "idle" /\ ~integrated /\ refused = {}
  /\ pc' = "pre" /\ made' = {}
  /\ UNCHANGED <<kind, gated, declared, task, target, archive, writes, cleaned, refused, pending, accepted, dirtAtAccept, integrated, manual, foreign>>

\* finish(): the dirt not covered by the plan is refused before any effect
\* (uncovered / _unexcused_dirty / _exception), then _initial_target_dirt().
\* An accepted task, or one with a pending merge, resumes at _integrate().
Pre ==
  /\ pc = "pre"
  /\ LET leftover == task \ ({"owned"} \cup archive) IN
     IF accepted THEN pc' = "integrate" /\ UNCHANGED refused
     ELSE IF leftover # {} THEN Refuse(leftover)
     ELSE IF TargetDirt # {} THEN Refuse(TargetDirt)
     ELSE pc' = "resolve" /\ UNCHANGED refused
  /\ UNCHANGED <<kind, gated, declared, task, target, archive, writes, made, cleaned, pending, accepted, dirtAtAccept, integrated, manual, foreign>>

\* _resolve_files(): archive entries leave the checkout.
Resolve ==
  /\ pc = "resolve"
  /\ task' = task \ archive
  /\ pc' = "commit"
  /\ UNCHANGED <<kind, gated, declared, target, archive, writes, made, cleaned, refused, pending, accepted, dirtAtAccept, integrated, manual, foreign>>

\* finish(): git add and git commit of the commit paths.
Commit ==
  /\ pc = "commit"
  /\ task' = task \ {"owned"}
  /\ pc' = "validate"
  /\ UNCHANGED <<kind, gated, declared, target, archive, writes, made, cleaned, refused, pending, accepted, dirtAtAccept, integrated, manual, foreign>>

\* finish(): `evidence = None if candidate else _run_validation(...)`; the
\* validator's own run inside _run_validation().
Validate ==
  /\ pc = "validate"
  /\ IF ~gated /\ writes
       THEN task' = task \cup {"valout"} /\ made' = made \cup {"valout"}
       ELSE UNCHANGED <<task, made>>
  /\ pc' = IF gated THEN "post" ELSE "clean"
  /\ UNCHANGED <<kind, gated, declared, target, archive, writes, cleaned, refused, pending, accepted, dirtAtAccept, integrated, manual, foreign>>

\* _run_validation(): after the run, remove the ignored files that are new and
\* match validation_outputs.
Clean ==
  /\ pc = "clean"
  /\ task' = task \ Sweep(task)
  /\ cleaned' = cleaned \cup Sweep(task)
  /\ pc' = "post"
  /\ UNCHANGED <<kind, gated, declared, target, archive, writes, made, refused, pending, accepted, dirtAtAccept, integrated, manual, foreign>>

\* finish() holds a run lease that lets other sessions of the task keep
\* running, so one can write while the validator runs.
Foreign ==
  /\ pc \in {"clean", "post"} /\ foreign < MaxForeign
  /\ task' = task \cup {"foreign"} /\ foreign' = foreign + 1
  /\ UNCHANGED <<pc, kind, gated, declared, target, archive, writes, made, cleaned, refused, pending, accepted, dirtAtAccept, integrated, manual>>

\* finish(): `final_dirty` after the validation; any is refused by _exception(),
\* and _created_hint() names the ones the run created. Otherwise the push and
\* the acceptance (or candidate) record.
Post ==
  /\ pc = "post"
  /\ IF task # {}
       THEN Refuse(task) /\ UNCHANGED <<accepted, dirtAtAccept>>
       ELSE pc' = "integrate" /\ accepted' = TRUE /\ dirtAtAccept' = task /\ UNCHANGED refused
  /\ UNCHANGED <<kind, gated, declared, task, target, archive, writes, made, cleaned, pending, integrated, manual, foreign>>

\* _integrate(): `_target_dirt(target_worktree, parent)` before the merge,
\* skipped while a merge intent is pending.
Integrate ==
  /\ pc = "integrate"
  /\ IF TargetDirt # {} /\ ~pending THEN Refuse(TargetDirt) ELSE pc' = "itValidate" /\ UNCHANGED refused
  /\ UNCHANGED <<kind, gated, declared, task, target, archive, writes, made, cleaned, pending, accepted, dirtAtAccept, integrated, manual, foreign>>

\* _integrate(): `_run_validation(target_worktree, item['validation'], ...)`.
ItValidate ==
  /\ pc = "itValidate"
  /\ IF writes /\ "valout" \notin target
       THEN target' = target \cup {"valout"} /\ made' = made \cup {"valout"}
       ELSE UNCHANGED <<target, made>>
  /\ pc' = "itClean"
  /\ UNCHANGED <<kind, gated, declared, task, archive, writes, cleaned, refused, pending, accepted, dirtAtAccept, integrated, manual, foreign>>

ItClean ==
  /\ pc = "itClean"
  /\ target' = target \ Sweep(target)
  /\ cleaned' = cleaned \cup Sweep(target)
  /\ pc' = "itCheck"
  /\ UNCHANGED <<kind, gated, declared, task, archive, writes, made, refused, pending, accepted, dirtAtAccept, integrated, manual, foreign>>

\* _integrate(): `changed = _target_snapshot(...)` raises 'integration changed
\* during final validation: <paths>'; the merge intent stays. Otherwise the push
\* and the integration record.
ItCheck ==
  /\ pc = "itCheck"
  /\ IF TargetDirt # {}
       THEN Refuse(TargetDirt) /\ pending' = TRUE /\ UNCHANGED integrated
       ELSE integrated' = TRUE /\ pc' = "done" /\ UNCHANGED <<refused, pending>>
  /\ UNCHANGED <<kind, gated, declared, task, target, archive, writes, made, cleaned, accepted, dirtAtAccept, manual, foreign>>

\* The agent follows the refusal: deletes the reproducible files it names
\* (guarded_delete), archives the private ones (a revised plan), and declares
\* the outputs it says the validation created (update-validation
\* --validation-outputs-json with the same argv).
Recover ==
  /\ pc = "idle" /\ refused # {}
  /\ task' = task \ (refused \cap Reproducible)
  /\ target' = target \ (refused \cap Reproducible)
  /\ archive' = archive \cup (refused \ Reproducible)
  /\ declared' = (declared \/ refused \cap made # {})
  /\ refused' = {}
  /\ UNCHANGED <<pc, kind, gated, writes, made, cleaned, pending, accepted, dirtAtAccept, integrated, manual, foreign>>

\* The agent runs the tests by hand in the task checkout.
Manual ==
  /\ pc = "idle" /\ ~accepted /\ manual < MaxManual
  /\ task' = task \cup {"manual"} /\ manual' = manual + 1
  /\ UNCHANGED <<pc, kind, gated, declared, target, archive, writes, made, cleaned, refused, pending, accepted, dirtAtAccept, integrated, foreign>>

\* update-validation to a validator that writes nothing. Not named by any refusal.
UpdateValidation ==
  /\ pc = "idle" /\ ~accepted /\ writes
  /\ writes' = FALSE
  /\ UNCHANGED <<pc, kind, gated, declared, task, target, archive, made, cleaned, refused, pending, accepted, dirtAtAccept, integrated, manual, foreign>>

Done == integrated /\ UNCHANGED vars

FinishSteps == Start \/ Pre \/ Resolve \/ Commit \/ Validate \/ Clean \/ Post \/ Integrate
               \/ ItValidate \/ ItClean \/ ItCheck

Next == FinishSteps \/ Recover \/ Foreign \/ Manual \/ UpdateValidation \/ Done

\* The agent keeps running finish and following its refusals; nothing makes
\* it replace the validator.
Spec == Init /\ [][Next]_vars /\ WF_vars(FinishSteps) /\ WF_vars(Recover)

------------------------------------------------------------------------------
\* Rules.

\* finish accepts only a checkout with no dirt left.
AcceptedClean == accepted => dirtAtAccept = {}

\* A refusal never names a declared output that the same run's validation created.
NoSelfInflictedRefusal == declared => refused \cap made = {}

\* finish never removes data another session wrote.
ForeignKept == "foreign" \notin cleaned

\* Running finish again and doing what each refusal names integrates the task.
FinishCompletes == <>integrated
==============================================================================
