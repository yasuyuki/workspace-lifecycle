---------------------------- MODULE FinishChart ----------------------------
(* A smaller instance of FinishIntegration for the chart: every validation
   passes, the owner runs finish with or without --wait-checks, and no other
   task integrates. *)
EXTENDS FinishIntegration

ChartInit == Init /\ valid = [c \in TaskCommits |-> TRUE] /\ validT = [c \in TaskCommits |-> TRUE]

ChartNext ==
  \/ \E m \in {"plain", "wait"} : Start(m)
  \/ FinEnter \/ FinValidate \/ FinPush \/ FinRecord
  \/ IntVerify \/ IntLock \/ IntMerge \/ IntValidate \/ IntGate \/ IntAccept \/ IntPush \/ IntRecord
  \/ Await \/ Withdraw \/ WithdrawPop
  \/ Kill \/ OwnerFix \/ CiCompletes \/ CiRerun \/ Done

ChartSpec == ChartInit /\ [][ChartNext]_vars
=============================================================================
