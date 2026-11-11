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

Steps ==
  \/ FinEnter \/ FinValidate \/ FinPush \/ FinRecord
  \/ IntVerify \/ IntLock \/ IntMerge \/ IntValidate \/ IntGate \/ IntAccept \/ IntPush \/ IntRecord
  \/ Await \/ Withdraw \/ WithdrawPop \/ OwnerFix \/ Done

\* The #80 story alone: finish --wait-checks killed while it waits, and the fix after it.
WaitKill == Kill /\ pc = "await"
FixChartNext == Start("wait") \/ Steps \/ WaitKill \/ CiCompletes
FixChartSpec == ChartInit /\ [][FixChartNext]_vars

\* The #93 story alone: finish killed after it recorded the check evidence,
\* before or after its push of the default checkout, and the fix after it.
\* The checks succeed.
PushKill == Kill /\ pc \in {"pushTarget", "record2"}
CiSucceeds == CiCompletes /\ \A c \in TaskCommits : ci'[c] # "failure"
CheckedFixChartNext == Start("plain") \/ Steps \/ PushKill \/ CiSucceeds
CheckedFixChartSpec == ChartInit /\ [][CheckedFixChartNext]_vars
=============================================================================
