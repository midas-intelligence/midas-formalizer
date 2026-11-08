import Lean

/- Exploratory only: execute against a fresh metavariable, publish no proof. -/
open Lean Elab Command Meta

namespace MidasProbe

-- A parsed string has its own byte offsets. Map them through the literal's
-- escapes before evaluating it, so errors and logged messages use displayed code.
private def literalOffsets (literal : Syntax) : Option (String × Array Nat) := do
  let value ← literal.isStrLit?
  let raw ← literal.isLit? strLitKind
  let start ← literal.getPos?
  let stop ← literal.getTailPos?
  let .original leading .. := literal.getHeadInfo | none
  -- A nested string may already have relocated offsets. Its escaped spelling
  -- then differs from this syntax token, so it needs its own displayed input.
  guard ((⟨leading.str, start, stop⟩ : Substring.Raw).toString == raw)
  let mut pos : String.Pos.Raw := ⟨1⟩
  let rawString := raw.startsWith "r"
  if rawString then
    while pos.get raw == '#' do pos := pos.next raw
    pos := pos.next raw
  let mut offsets := #[]
  let mut decoded := ""
  while decoded.utf8ByteSize < value.utf8ByteSize do
    let beginning := pos
    let mut char := pos.get raw
    pos := pos.next raw
    if !rawString && char == '\\' then
      if let some (escaped, next) := Syntax.decodeQuotedChar raw pos then
        char := escaped
        pos := next
      else
        pos ← Syntax.decodeStringGap raw pos
        continue
    decoded := decoded.push char
    for _ in [:char.utf8Size] do offsets := offsets.push (start.byteIdx + beginning.byteIdx)
  guard (decoded == value)
  return (value, offsets.push (start.byteIdx + pos.byteIdx))

private partial def literalLocations (stx : Syntax) : Array (String × Array Nat) := Id.run do
  if let some entry := literalOffsets stx then return #[entry]
  return stx.getArgs.foldl (fun entries child => entries ++ literalLocations child) #[]

private def relocateSyntax (stx : Syntax) (input outer : String) (offsets : Array Nat) : Syntax :=
  stx.rewriteBottomUp fun node => Id.run do
    let some info := node.getInfo? | return node
    let mapPos := fun (pos : String.Pos.Raw) => String.Pos.Raw.mk (offsets[pos.byteIdx]?.getD outer.utf8ByteSize)
    let mapSpace := fun (space : Substring.Raw) =>
      { space with str := outer, startPos := mapPos space.startPos, stopPos := mapPos space.stopPos }
    let info := match info with
      | .original leading pos trailing stop =>
        if leading.str == input then .original (mapSpace leading) (mapPos pos) (mapSpace trailing) (mapPos stop)
        else info
      | .synthetic pos stop canonical => .synthetic (mapPos pos) (mapPos stop) canonical
      | .none => .none
    return node.setInfo info

abbrev InputSources := IO.Ref (Array (String × String))

private def inputSource (sources : InputSources) (parent text : String) : IO String := do
  let entries ← sources.get
  if let some entry := entries.find? (fun entry => entry.2 == text) then return entry.1
  let parent := (parent.splitOn ".input").head!
  let label := s!"{parent}.input{entries.size + 1}"
  sources.set (entries.push (label, text))
  return label

private def evalParsedTactic (sources : InputSources) (literals : Array (String × Array Nat))
    (outer : String) (stx : Syntax) : Tactic.TacticM Unit := do
  let .original leading .. := stx.getHeadInfo | Tactic.evalTactic stx
  let input := leading.str
  let ctx ← readThe Core.Context
  if input == ctx.fileMap.source then return ← Tactic.evalTactic stx
  -- Parser choice nodes may contain the same literal span more than once.
  let matching := (literals.filter (fun entry => entry.1 == input)).toList.eraseDups.toArray
  if matching.size == 1 && ctx.fileMap.source == outer then
    return ← Tactic.evalTactic (relocateSyntax stx input outer matching[0]!.2)
  -- Computed strings and duplicate literals have no unique displayed span.
  -- Keep their own source map and publish that exact input with the result.
  let label ← inputSource sources ctx.fileName input
  withTheReader Core.Context (fun ctx => { ctx with fileName := label, fileMap := FileMap.ofString input }) do
    let _ := MonadAlwaysExcept.except (m := Tactic.TacticM)
    try Tactic.evalTactic stx catch error =>
      if error.isRuntime then throw error
      let detail ← (← addMessageContext error.toMessageData).toString
      let ref := error.getRef
      let ref := if let some range := ref.getRange? then
        ref.setInfo (.original ⟨input, range.start, range.start⟩ range.start
          ⟨input, range.stop, range.stop⟩ range.stop)
        else ref
      throw <| Exception.error ref (.ofFormat (Format.text detail))

private def evalLocatedRunTac (sources : InputSources) : Tactic.Tactic := fun stx => do
  let `(tactic| run_tac $body:doSeq) := stx | throwUnsupportedSyntax
  let outer := (← getFileMap).source
  let action ← liftM (m := TermElabM) <| withoutModifyingEnv do
    let type := mkApp (mkConst ``Tactic.TacticM) (mkConst ``Unit)
    let value ← Term.elabTermEnsuringType (← `(discard do $body)) type
    Term.synthesizeSyntheticMVarsNoPostponing
    let value ← instantiateMVars value
    if ← Term.logUnassignedUsingErrorInfos (← getMVars value) then throwAbortTerm
    if !value.getUsedConstants.contains ``Tactic.evalTactic then
      return ← unsafe Meta.evalExpr (Tactic.TacticM Unit) type value
    -- Pass a host closure into the compiled action. Frozen source environments
    -- need no checker declarations, and generated code stays constant-sized.
    let evalType ← inferType (mkConst ``Tactic.evalTactic)
    withLocalDeclD `probeEvalTactic evalType fun evaluate => do
      let value := value.replace fun expr =>
        if expr.isConstOf ``Tactic.evalTactic then some evaluate else none
      let value ← mkLambdaFVars #[evaluate] value
      let compiled ← unsafe Meta.evalExpr ((Syntax → Tactic.TacticM Unit) → Tactic.TacticM Unit)
        (← inferType value) value
      return compiled (evalParsedTactic sources (literalLocations stx) outer)
  action

-- Lean quotations normally give every generated node the current runtime ref,
-- which is the enclosing run_tac, rather than the quoted tactic's source span.
-- Keep Lean's hygienic quotation expansion and replace only its source info.
-- The emitted expression uses Lean primitives so it also works in frozen source
-- environments that do not contain the checker's MidasProbe declarations.
private partial def quotationPositions (source : Syntax) : StateM (Array String) Unit := do
  for arg in source.getArgs do quotationPositions arg
  let positions := match source.getRange? with
    | some range => s!"{range.start.byteIdx} {range.stop.byteIdx}"
    | none => "- -"
  modify (·.push positions)

private def locatedSyntax (source : Syntax) (value : Term) : TermElabM Term := do
  -- Keep the generated program constant-sized. Emitting getArg/setArg/setInfo
  -- calls for every node exhausts the code generator's inliner on large quotes.
  -- A single literal holds byte offsets in postorder; missing ranges keep the
  -- quotation's default info. The traversal preserves identifiers and hygiene.
  let positions := String.intercalate " " ((quotationPositions source).run #[]).2.toList
  `(((Lean.Syntax.rewriteBottomUpM (m := StateM (List String)) (fun node => do
    match (← get) with
    | start :: stop :: rest =>
      set rest
      match start.toNat?, stop.toNat? with
      | some start, some stop =>
        pure (Lean.Syntax.setInfo (Lean.SourceInfo.synthetic ⟨start⟩ ⟨stop⟩ true) node)
      | _, _ => pure node
    | _ => pure node) $value).run' ($(quote positions).splitOn " ")))

private partial def hasAntiquotation (stx : Syntax) : Bool :=
  stx.isAntiquot || stx.antiquotSpliceKind?.isSome ||
    stx.antiquotSuffixSplice?.isSome || stx.getArgs.any hasAntiquotation

private def elabLocatedTacticQuotation : Term.TermElab := fun stx expected? => do
  let source := stx.getQuotContent
  -- Antiquotation can change the tree shape or supply syntax from another input.
  -- Do not guess source positions for those dynamically constructed nodes.
  if hasAntiquotation source then throwUnsupportedSyntax
  let kind ← Term.Quotation.getQuotKind stx
  let expanded : Term := ⟨← Term.Quotation.stxQuot.expand stx⟩
  let value := mkIdent (← mkFreshUserName `probeSyntax)
  let located ← locatedSyntax source (← `(($value).raw))
  let expanded ← `(do
    let $value:ident ← $expanded:term
    pure (@Lean.TSyntax.mk $(quote kind) $located))
  Term.elabTerm expanded expected?

private def withLocatedQuotations (sources : InputSources) (action : TermElabM α) : TermElabM α := do
  let attr := Term.termElabAttribute
  let previous := attr.ext.getState (← getEnv)
  let tactics := Tactic.tacticElabAttribute
  let previousTactics := tactics.ext.getState (← getEnv)
  -- The checker restores the frozen source environment before probing. Install
  -- closures locally instead of importing checker declarations into that scope.
  modifyEnv fun env => attr.ext.modifyState env fun state =>
    [``Lean.Parser.Tactic.quot, ``Lean.Parser.Tactic.quotSeq].foldl (fun state key =>
      state.insert {
        key, declName := ``elabLocatedTacticQuotation,
        isBuiltin := false, value := elabLocatedTacticQuotation }) state
  -- Replace the builtin handler, rather than adding a fallback alternative:
  -- an ordinary proof failure must not rerun the uninstrumented action.
  modifyEnv fun env => tactics.ext.modifyState env fun state =>
    let key := ``Lean.Parser.Tactic.runTac
    { state with table := state.table.insert key ((state.table.findD key []).map fun entry =>
        if entry.declName == ``Tactic.evalRunTac then { entry with value := evalLocatedRunTac sources }
        else entry) }
  try action finally
    modifyEnv fun env => attr.ext.modifyState env fun _ => previous
    modifyEnv fun env => tactics.ext.modifyState env fun _ => previousTactics

structure Case where
  id : String
  operation : String := "tactic"
  code : String
  setup : String := ""
  expected : Option String := none
  repairs : Array String := #[]
  deriving FromJson, ToJson

def parseTerm (text : String) : TermElabM Syntax := do
  match Parser.runParserCategory (← getEnv) `term text (← getFileName) with
  | .ok stx => return stx
  | .error error => throwError "{error}"

private def parseTactics (env : Environment) (fileName text : String) :
    Except (String × Bool) Syntax := Id.run do
  let ictx := Parser.mkInputContext text fileName
  let parser := Parser.andthenFn Parser.whitespace Parser.Tactic.tacticSeq.fn
  let state := parser.run ictx { env, options := {} } (Parser.getTokenTable env) (Parser.mkParserState text)
  unless state.allErrors.isEmpty do
    return .error (state.toErrorMsg ictx,
      state.allErrors.any fun (_, _, error) => error.expected.contains "')'")
  unless ictx.atEnd state.pos do
    return .error ((state.mkError "end of input").toErrorMsg ictx, false)
  return .ok state.stxStack.back

-- Pair parser positions with the exact input, never a synthetic `by` wrapper.
-- Render exceptions while the snippet's file map and local context are live.
def inSnippet (sources : InputSources) (id stage text : String) (action : TermElabM α) : TermElabM α :=
  withTheReader Core.Context (fun ctx => { ctx with
    fileName := id ++ "." ++ stage, fileMap := FileMap.ofString text,
    ref := Syntax.missing }) do
    let _ := MonadAlwaysExcept.except (m := TermElabM)
    try withLocatedQuotations sources action catch error =>
      if error.isRuntime then throw error
      let detail ← (← addMessageContext error.toMessageData).toString
      if let some offset := error.getRef.getPos? then
        if let .original leading .. := error.getRef.getHeadInfo then
          if leading.str != text then
            let label ← inputSource sources (id ++ "." ++ stage) leading.str
            let pos := (FileMap.ofString leading.str).toPosition offset
            throwError "{label}:{pos.line}:{pos.column}: {detail}"
        let pos := (FileMap.ofString text).toPosition offset
        throwError "{id}.{stage}:{pos.line}:{pos.column}: {detail}"
      else
        throwError "[{id}.{stage}] {detail}"

def snippetTactics (sources : InputSources) (id stage text : String) (repairs : Array String := #[])
    (usedCode : Option (IO.Ref String) := none) : Tactic.TacticM Unit := do
  if text.trimAscii.toString.isEmpty then return
  let env ← getEnv
  let mut code := text
  let mut parsed := parseTactics env (id ++ "." ++ stage) text
  if let .error (_, true) := parsed then
    for candidate in repairs[:min repairs.size 16] do
      if candidate.utf8ByteSize > 16000 then continue
      if let .ok stx := parseTactics env (id ++ "." ++ stage) candidate then
        code := candidate
        parsed := .ok stx
        break
  if let some usedCode := usedCode then usedCode.set code
  let ctx ← read
  let state ← get
  let action : Tactic.TacticM Unit := do
    match parsed with
    | .ok stx => Tactic.evalTactic stx
    | .error (error, _) => throwError "{error}"
  let (_, state) ← inSnippet sources id stage code <| (action.run ctx).run state
  set state

def render (e : Expr) : MetaM String := do
  return (← ppExpr (← instantiateMVars e)).pretty

def runCase (expected : Expr) (spec : Case) (usedCode : IO.Ref String) (sources : InputSources) : TermElabM Json :=
  Term.withoutErrToSorry do
    Term.synthesizeSyntheticMVarsNoPostponing
    let goal ← mkFreshExprMVar expected
    let answer ← IO.mkRef (Json.mkObj [])
    let goals ← Tactic.run goal.mvarId! <| Tactic.withoutRecover do
      snippetTactics sources spec.id "setup" spec.setup
      if spec.operation == "tactic" then
        snippetTactics sources spec.id "code" spec.code spec.repairs (some usedCode)
      else
        let goals ← Tactic.getUnsolvedGoals
        unless goals.length == 1 do
          throwError "expression inspection requires exactly one goal after setup"
        goals.head!.withContext do
          let expected? ← spec.expected.mapM fun text =>
            inSnippet sources spec.id "expected" text do Term.elabType (← parseTerm text)
          let value ← inSnippet sources spec.id "code" spec.code do
            let stx ← parseTerm spec.code
            let value ← if spec.operation == "synth" then synthInstance (← Term.elabType stx)
              else if spec.operation == "term" then Term.elabTermEnsuringType stx expected?
              else throwError "unknown probe operation: {spec.operation}"
            Term.synthesizeSyntheticMVarsNoPostponing
            return value
          let value ← instantiateMVars value
          answer.set <| Json.mkObj [
            ("expression", toJson (← render value)),
            ("inferred_type", toJson (← render (← inferType value))),
            ("contains_sorry", toJson value.hasSorry),
            ("unresolved_metavariables", toJson value.hasMVar)]
    Term.synthesizeSyntheticMVarsNoPostponing
    let remaining ← goals.filterM fun goal => return !(← goal.isAssigned)
    let rendered ← remaining.mapM fun goal => return (← ppGoal goal).pretty
    let value ← instantiateMVars goal
    let inspection ← answer.get
    let inspectionSorry := (inspection.getObjValAs? Bool "contains_sorry").toOption.getD false
    let status := if value.hasSorry || inspectionSorry then "contains_sorry"
      else if spec.operation != "tactic" then "inspected"
      else if remaining.isEmpty && !value.hasMVar then "closed_exploratory"
      else "open"
    return Json.mkObj [
      ("id", toJson spec.id), ("operation", toJson spec.operation),
      ("status", toJson status), ("goals", toJson rendered),
      ("contains_sorry", toJson (value.hasSorry || inspectionSorry)),
      ("unresolved_metavariables", toJson value.hasMVar),
      ("inspection", ← answer.get)]

def run (expected : Expr) (spec : Case) (aliases : Nat := 0) : CommandElabM Json := do
  modify fun state => { state with messages := {} }
  liftTermElabM <| withCurrHeartbeats do
    let _ := MonadAlwaysExcept.except (m := TermElabM)
    let usedCode ← IO.mkRef spec.code
    let sources ← IO.mkRef #[]
    let result ← try
        -- Core counts raw heartbeats, 1,000 times the configured option's units.
        let limit := maxHeartbeats.get (← getOptions)
        withTheReader Core.Context (fun ctx => { ctx with maxHeartbeats := limit * 1000 }) do
          forallBoundedTelescope expected aliases fun _ target => runCase target spec usedCode sources
      catch error =>
        pure <| Json.mkObj [("id", toJson spec.id), ("operation", toJson spec.operation),
          ("status", toJson (if error.isRuntime then "interrupted" else "error")),
          ("diagnostic", toJson (← (← addMessageContext error.toMessageData).toString))]
    let log ← Core.getMessageLog
    let result := if log.hasErrors then result.setObjVal! "status" (toJson "error") else result
    let messages ← log.toList.mapM fun message => do
      return Json.mkObj [("severity", toJson (toString message.severity)),
        ("file", toJson message.fileName), ("line", toJson message.pos.line),
        ("column", toJson message.pos.column), ("text", toJson (← message.data.toString))]
    let mut row := Json.mkObj [("result", result), ("messages", toJson messages)]
    let diagnostic := (result.getObjValAs? String "diagnostic").toOption.getD ""
    let inputs := (← sources.get).filter fun (label, _) =>
      diagnostic.startsWith (label ++ ":") || log.toList.any (fun message => message.fileName == label)
    if !inputs.isEmpty then
      row := row.setObjVal! "sources" (toJson (inputs.map fun (label, text) =>
        Json.mkObj [("file", toJson label), ("code", toJson text)]))
    let code ← usedCode.get
    return if code == spec.code then row else row.setObjVal! "code" (toJson code)

end MidasProbe
