# 作業の push 判定

一つの task が、自分の repository への push を判定するために保持するコマンドの語彙。

## Language

**Preflight**:
task が保持する、その task の repository への push を判定するためのコマンド。task を始めるときに呼び手が選ぶ。引数の一つは repository placeholder そのものでなければならない。
_Avoid_: push の判定結果、install の固定、policy file

**Repository placeholder**:
preflight の引数のうち、値そのものが `{repo}` であるもの。判定のとき、その引数だけが判定対象の checkout になる。文字列の途中に `{repo}` を含む path は普通の引数である。
_Avoid_: path の雛形、埋め込みの `{repo}`

**Lifecycle interpreter placeholder**:
preflight の引数のうち、値そのものが `{lifecycle_python}` であるもの。判定のとき、その引数は running lifecycle の interpreter になる。preflight がこれを使えば、install が移っても変わっても preflight replacement は要らない。文字列の途中に含むものは普通の引数である。
_Avoid_: install の path、固定した python、外部 runtime の interpreter

**Task**:
現在の作業として、preflight をちょうど一つ持つ名前付きの仕事。受け入れや統合をしても preflight は残る。退役が完了するとその task は現在の作業ではなくなり、preflight も残らない。
_Avoid_: branch、worktree、install

**Preflight replacement**:
一つの現在の task について、いま保持しているコマンドそのもの、新しいコマンド、根拠を示して preflight を取り替えること。受け入れ済みでも取り替えられる。退役を始めている task は取り替えられない。複数 task の一括移行ではない。
_Avoid_: migration、一括更新、validation の変更

**Running lifecycle**:
push を判定している lifecycle のプロセス。task はそのプロセスを記録せず、lifecycle interpreter placeholder を通じてだけ指す。
_Avoid_: install、固定した python

**Install**:
配置された lifecycle の一つの実体。呼び手が preflight の中にその実体の interpreter を書くことがある。その記述は呼び手が書いたコマンドの一部であり、task が別に持つ事実ではない。そう書いた task は、その install が退役する前に preflight replacement が要る。
_Avoid_: running lifecycle、task

### 検証

**Validation input**:
一回の validation が読みうるものの全体。task の validation argv、実行する checkout、その checkout の HEAD commit（tree だけでなく message・parents などの metadata を含む）と、index・未追跡・ignored・preserved data を含む作業中の内容である。tree が同じでも、checkout が違えば、また merge 中の状態とその merge commit のように HEAD commit が違えば、同じ validation input ではない。
_Avoid_: tree hash、同じ commit

**Reused validation**:
同じ finish の中で、validation input が直前に成功した run と変わっていない段階が、その run を自分の証拠とすること。receipt は再利用元の段階を示し、新しい run とは書かない。別の finish、別の checkout、別の HEAD commit、失敗した run からは再利用しない。
_Avoid_: cache、省略された validation、検証済み tree

**Prepared integration**:
受入 commit の required check が成功する前に、統合先 checkout で fast-forward または merge を行い validation まで済ませた、まだ push していない統合。check が成功すれば同じ finish がそれを push し、失敗・期限切れ・拒否なら統合先を準備前の HEAD に戻して取り下げる。統合済みではない。
_Avoid_: 先行 push、仮統合、統合済み

**Candidate**:
required check を要する task の finish が、task checkout で validation をせずに push した commit。受入ではない。その commit の required check が成功し、統合先 checkout がこれから push する commit の validation に成功したとき、同じ記録が受入になる。それまで dependency・child の統合・退役・owner receipt は、これを受入として扱わない。統合先が進んでいて merge commit を作る場合、統合先で validation されるのは merge commit であり、candidate そのものは validation input にならない。candidate は topic の required check で確かめ、統合先へ届く内容は merge commit の validation で確かめるので、この差を許容する。
_Avoid_: 受入済み、検証済み commit、仮受入

### 統合先

**Target ignored data**:
default branch の checkout にある ignored の file や directory（cache、build 出力、実験状態など）。その checkout はどの task より長く残るので、どの task のものでもない。統合先の dirty ではなく、統合はこれを変えずに残す。parent task の checkout にある ignored data はこれに当たらず、dirty のままである。
_Avoid_: 作業の残り、統合先の dirty、消してよい生成物

**Ignored collision**:
統合先へ入る変更の path、またはその親 directory が、target ignored data・link・大文字小文字だけ違う別名と重なること。入る file が ignored data を含む tracked directory を置き換える場合も含む。Git はこのとき ignored data を上書きしたり消したりしうるので、統合はその path と理由を示して拒否する。拒否しても pending の統合は残り、data を退けて同じ finish を繰り返せば続く。
_Avoid_: dirty、conflict

**Fast-forward merge**:
default branch の checkout へ入る merge commit を、その checkout の外で作り、checkout はそこへ fast-forward するだけで進む統合。checkout を書き換える Git 自身が、書く瞬間に target ignored data を見つけて拒否する。検査の後に別の process が書いた data もこれで守られる。通常の merge と同じ tree・parent の順・message を持つ。
_Avoid_: 検査してから merge、squash、rebase

**Operator-started merge**:
衝突する merge、または commit や merge の hook を持つ repository への merge。fast-forward merge にできないので lifecycle は始めず、その checkout の operator が通常の Git merge として始める。始めた merge は同じ finish が引き継いで commit する。
_Avoid_: 自動の衝突解決、中断した統合

### Required checks

**Required check state**:
受入 commit そのものについて、required check の app と name が一致する最新の GitHub check run が示す状態。一致する run が無ければ missing である。別の commit・別の app・別の名前の check、workflow run の成否はこの状態にならない。
_Avoid_: CI の結果、workflow run の status

**Pending check**:
state が missing、または未完了（queued、in_progress など）の required check。待つ理由になるが、成功ではない。
_Avoid_: 実行中の workflow

**Failed check**:
完了していて、conclusion が success でない required check。failure・cancelled・timed_out のほか skipped・neutral・action_required も含む。一つでもあれば、ほかの check の完了を待たずに統合しない。
_Avoid_: run 全体の失敗

**Integration check state**:
統合が remote default branch へ push した commit そのものについての、required check の状態。その branch への push で作られた check run だけが示す。fast-forward で受入 commit がそのまま統合 commit になっても、task branch への push で作られた run は含まない。parent task への統合 commit は対象にしない。受入 commit の required check state とは別の事実であり、失敗しても統合済みという事実は変わらない。required check が一つも宣言されていなければ undeclared であり、成功ではない。
_Avoid_: main の CI 結果、統合の成否、受入 commit の check

### 回収

**Object identity**:
退役が記録する、file や directory 一つの同一性。filesystem の中の inode と、その filesystem を指す値からなる。filesystem を指す値には filesystem ID（ext4 では UUID から決まる）と device 番号がある。device 番号は boot ごとに振り直されうるので、記録に filesystem ID があればそれで filesystem を確かめる。filesystem ID が無い記録や 0 の記録は、device 番号で確かめる。内容が同じでも、inode か filesystem が違えば別の object である。
_Avoid_: path、inode だけ、digest の一致

**Mount session**:
mount ID が意味を持つ範囲。boot と mount namespace の組であり、再起動や headless 入口の呼出しごとに変わりうる。mount ID は、同じ mount session の記録とだけ比べる。別の mount session から回収するときも、member がすべて root と同じ mount にあることは確かめる。
_Avoid_: boot ID だけ、接続
