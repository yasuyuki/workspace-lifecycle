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
