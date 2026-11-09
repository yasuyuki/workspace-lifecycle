---------------------------- MODULE ValidationUpdate ----------------------------
(* The task checkout part of service.py finish(), update_validation() and
   update_preflight(). finish opens an intent, runs one plan restore action,
   stages and commits the plan's source, validates and pushes through the
   task's preflight; it can be killed at any step. update_validation replaces
   a validator that fails and update_preflight a preflight that fails; each
   releases the finish intent only when it recorded no effect. One action is
   one saved state change or one Git or filesystem effect. *)
EXTENDS Naturals

CONSTANTS
  Restore,     \* the plan has a restore entry (_resolve_files)
  CommitPlan,  \* the plan has commit entries
  MaxKills

None == "none"
NoIntent == [kind |-> None, initial |-> None, phase |-> None, tree |-> FALSE, committed |-> FALSE]

VARIABLES
  head,       \* task checkout HEAD: c0 before the finish commit, c1 after it
  staged,     \* the plan's source is in the index
  restored,   \* the restore entry's file is gone (_resolve_files removed it)
  validator,  \* the task's validation argv: bad (fails) or good
  preflight,  \* the task's preflight argv: bad (fails before any push) or good
  intent,     \* state['intents'][task]: initial_head, restore action phase, commit_tree, committed
  accepted,   \* task['acceptance'] saved
  pc,
  kills

vars == <<head, staged, restored, validator, preflight, intent, accepted, pc, kills>>

Init ==
  /\ head = "c0" /\ staged = FALSE /\ restored = FALSE
  /\ validator \in {"bad", "good"} /\ preflight \in {"bad", "good"}
  /\ intent = NoIntent /\ accepted = FALSE /\ pc = "idle" /\ kills = 0

----------------------------------------------------------------------------
(* finish() *)

Start ==
  /\ pc = "idle" /\ ~accepted
  /\ pc' = "enter"
  /\ UNCHANGED <<head, staged, restored, validator, preflight, intent, accepted, kills>>

\* Lines 1179-1197: open the intent, or adopt a commit that matches commit_tree.
Enter ==
  /\ pc = "enter"
  /\ CASE intent.kind = None ->
            intent' = [NoIntent EXCEPT !.kind = "finish", !.initial = head] /\ pc' = "restore"
       [] head = intent.initial \/ intent.committed ->
            pc' = "restore" /\ UNCHANGED intent
       [] intent.tree /\ head = "c1" /\ intent.initial = "c0" ->
            intent' = [intent EXCEPT !.committed = TRUE] /\ pc' = "restore"
       [] OTHER -> pc' = "idle" /\ UNCHANGED intent
  /\ UNCHANGED <<head, staged, restored, validator, preflight, accepted, kills>>

\* _resolve_files: the action phase is saved before and after its effect.
RestoreIntent ==
  /\ pc = "restore"
  /\ IF ~Restore \/ intent.phase = "resolved" THEN pc' = "stage" /\ UNCHANGED intent
     ELSE IF intent.phase = "restoring" THEN pc' = "restoreEffect" /\ UNCHANGED intent
     ELSE intent' = [intent EXCEPT !.phase = "restoring"] /\ pc' = "restoreEffect"
  /\ UNCHANGED <<head, staged, restored, validator, preflight, accepted, kills>>

RestoreEffect ==
  /\ pc = "restoreEffect"
  /\ restored' = TRUE /\ pc' = "restoreDone"
  /\ UNCHANGED <<head, staged, validator, preflight, intent, accepted, kills>>

RestoreDone ==
  /\ pc = "restoreDone"
  /\ intent' = [intent EXCEPT !.phase = "resolved"] /\ pc' = "stage"
  /\ UNCHANGED <<head, staged, restored, validator, preflight, accepted, kills>>

\* Lines 1252-1253: git add of the commit paths.
Stage ==
  /\ pc = "stage"
  /\ IF ~CommitPlan \/ intent.committed THEN pc' = "validate" /\ UNCHANGED staged
     ELSE staged' = TRUE /\ pc' = "tree"
  /\ UNCHANGED <<head, restored, validator, preflight, intent, accepted, kills>>

\* Line 1257: commit_tree saved.
Tree ==
  /\ pc = "tree"
  /\ intent' = [intent EXCEPT !.tree = TRUE] /\ pc' = "commit"
  /\ UNCHANGED <<head, staged, restored, validator, preflight, accepted, kills>>

\* Line 1260: git commit.
Commit ==
  /\ pc = "commit"
  /\ head' = "c1" /\ staged' = FALSE /\ pc' = "committed"
  /\ UNCHANGED <<restored, validator, preflight, intent, accepted, kills>>

\* Line 1265: committed saved.
Committed ==
  /\ pc = "committed"
  /\ intent' = [intent EXCEPT !.committed = TRUE] /\ pc' = "validate"
  /\ UNCHANGED <<head, staged, restored, validator, preflight, accepted, kills>>

\* Lines 1269-1270: a failing validator leaves the intent for update-validation.
Validate ==
  /\ pc = "validate"
  /\ pc' = IF validator = "good" THEN "push" ELSE "idle"
  /\ UNCHANGED <<head, staged, restored, validator, preflight, intent, accepted, kills>>

\* Line 1291, _push(): a preflight that fails pushes nothing and leaves the
\* intent for update-preflight.
Push ==
  /\ pc = "push"
  /\ pc' = IF preflight = "good" THEN "record" ELSE "idle"
  /\ UNCHANGED <<head, staged, restored, validator, preflight, intent, accepted, kills>>

\* Lines 1292-1312: save the acceptance, dropping the intent.
Record ==
  /\ pc = "record"
  /\ accepted' = TRUE /\ intent' = NoIntent /\ pc' = "idle"
  /\ UNCHANGED <<head, staged, restored, validator, preflight, kills>>

Kill ==
  /\ pc # "idle" /\ kills < MaxKills
  /\ pc' = "idle" /\ kills' = kills + 1
  /\ UNCHANGED <<head, staged, restored, validator, preflight, intent, accepted>>

----------------------------------------------------------------------------
(* The corrections, with the owner's current HEAD *)

\* _effectless_finish: a finish intent that recorded no effect at this HEAD.
Effectless ==
  intent.kind = "finish" /\ intent.phase = None /\ ~intent.tree /\ ~intent.committed
  /\ intent.initial = head

\* _recorded_finish: every recorded effect completed at this HEAD.
Recorded ==
  /\ intent.kind = "finish" /\ intent.phase \in {None, "resolved"}
  /\ IF ~intent.committed THEN ~intent.tree /\ intent.initial = head
     ELSE head = "c1" /\ intent.initial = "c0"

\* update_validation(); enabled only when the update succeeds, a refusal changes nothing.
UpdateValidation ==
  /\ pc = "idle" /\ ~accepted /\ validator = "bad"
  /\ intent.kind = None \/ ((Effectless \/ Recorded) /\ ~staged)
  /\ validator' = "good"
  /\ intent' = IF Effectless THEN NoIntent ELSE intent
  /\ UNCHANGED <<head, staged, restored, preflight, accepted, pc, kills>>

\* update_preflight() and _correctable_finish(); enabled only when the update succeeds.
UpdatePreflight ==
  /\ pc = "idle" /\ preflight = "bad"
  /\ intent.kind = None \/ ((Effectless \/ Recorded) /\ ~staged)
  /\ preflight' = "good"
  /\ intent' = IF Effectless THEN NoIntent ELSE intent
  /\ UNCHANGED <<head, staged, restored, validator, accepted, pc, kills>>

Done ==
  /\ pc = "idle" /\ accepted
  /\ UNCHANGED vars

Progress ==
  \/ Start \/ Enter \/ RestoreIntent \/ RestoreEffect \/ RestoreDone \/ Stage
  \/ Tree \/ Commit \/ Committed \/ Validate \/ Push \/ Record

Next == Progress \/ Kill \/ UpdateValidation \/ UpdatePreflight \/ Done

\* The owner keeps running finish, and runs each correction whenever it can succeed.
Spec == Init /\ [][Next]_vars /\ WF_vars(Progress)
        /\ SF_vars(UpdateValidation) /\ SF_vars(UpdatePreflight)

----------------------------------------------------------------------------
(* Rules *)

\* A file the finish removed and a commit the finish made stay recorded by its
\* intent until the acceptance, so no correction drops a finish that left an effect.
NoUnrecordedEffect ==
  ~accepted =>
    /\ restored => intent.phase \in {"restoring", "resolved"}
    /\ head = "c1" => intent.tree

\* After a validation or preflight failure, the corrections and the same finish
\* always reach the acceptance.
FinishCompletes == <>accepted
=============================================================================
