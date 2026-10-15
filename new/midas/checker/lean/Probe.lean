import Lean

/- Exploratory only: execute against a fresh metavariable, publish no proof. -/
open Lean Elab Command Meta

namespace MidasProbe

-- Lean quotations normally give every generated node the current runtime ref,
-- which is the enclosing run_tac, rather than the quoted tactic's source span.
-- Keep Lean's hygienic quotation expansion and replace only its source info.
-- The emitted expression uses Lean primitives so it also works in frozen source
-- environments that do not contain the checker's MidasProbe declarations.
private partial def locatedSyntax (source : Syntax) (value : Term) : TermElabM Term := do
  let mut result := value
  for i in [:source.getNumArgs] do
    let arg ← locatedSyntax source[i] (← `(Lean.Syntax.getArg $value $(quote i)))
    result ← `(Lean.Syntax.setArg $result $(quote i) $arg)
  if let some range := source.getRange? then
    result ← `(Lean.Syntax.setInfo
      (Lean.SourceInfo.synthetic ⟨$(quote range.start.byteIdx)⟩ ⟨$(quote range.stop.byteIdx)⟩ true) $result)
  return result

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

private def withLocatedQuotations (action : TermElabM α) : TermElabM α := do
  let attr := Term.termElabAttribute
  let previous := attr.ext.getState (← getEnv)
  -- The checker restores the frozen source environment before probing. Install
  -- closures locally instead of importing checker declarations into that scope.
  modifyEnv fun env => attr.ext.modifyState env fun state =>
    [``Lean.Parser.Tactic.quot, ``Lean.Parser.Tactic.quotSeq].foldl (fun state key =>
      state.insert {
        key, declName := ``elabLocatedTacticQuotation,
        isBuiltin := false, value := elabLocatedTacticQuotation }) state
  try action finally
    modifyEnv fun env => attr.ext.modifyState env fun _ => previous

structure Case where
  id : String
  operation : String := "tactic"
  code : String
  setup : String := ""
  expected : Option String := none
  deriving FromJson, ToJson

def parseTerm (text : String) : TermElabM Syntax := do
  match Parser.runParserCategory (← getEnv) `term text (← getFileName) with
  | .ok stx => return stx
  | .error error => throwError "{error}"

def runTactics (text : String) : Tactic.TacticM Unit := do
  if text.trimAscii.toString.isEmpty then return
  let env ← getEnv
  let ictx := Parser.mkInputContext text (← getFileName)
  let parser := Parser.andthenFn Parser.whitespace Parser.Tactic.tacticSeq.fn
  let state := parser.run ictx { env, options := {} } (Parser.getTokenTable env) (Parser.mkParserState text)
  unless state.allErrors.isEmpty do throwError "{state.toErrorMsg ictx}"
  unless ictx.atEnd state.pos do throwError "{(state.mkError "end of input").toErrorMsg ictx}"
  Tactic.evalTactic state.stxStack.back

-- Pair parser positions with the exact input, never a synthetic `by` wrapper.
-- Render exceptions while the snippet's file map and local context are live.
def inSnippet (id stage text : String) (action : TermElabM α) : TermElabM α :=
  withTheReader Core.Context (fun ctx => { ctx with
    fileName := id ++ "." ++ stage, fileMap := FileMap.ofString text,
    ref := Syntax.missing }) do
    let _ := MonadAlwaysExcept.except (m := TermElabM)
    try withLocatedQuotations action catch error =>
      if error.isRuntime then throw error
      let detail ← (← addMessageContext error.toMessageData).toString
      if let some offset := error.getRef.getPos? then
        let pos := (FileMap.ofString text).toPosition offset
        throwError "{id}.{stage}:{pos.line}:{pos.column}: {detail}"
      else
        throwError "[{id}.{stage}] {detail}"

def snippetTactics (id stage text : String) : Tactic.TacticM Unit := do
  let ctx ← read
  let state ← get
  let (_, state) ← inSnippet id stage text <| ((runTactics text).run ctx).run state
  set state

def render (e : Expr) : MetaM String := do
  return (← ppExpr (← instantiateMVars e)).pretty

def runCase (expected : Expr) (spec : Case) : TermElabM Json :=
  Term.withoutErrToSorry do
    Term.synthesizeSyntheticMVarsNoPostponing
    let goal ← mkFreshExprMVar expected
    let answer ← IO.mkRef (Json.mkObj [])
    let goals ← Tactic.run goal.mvarId! <| Tactic.withoutRecover do
      snippetTactics spec.id "setup" spec.setup
      if spec.operation == "tactic" then
        snippetTactics spec.id "code" spec.code
      else
        let goals ← Tactic.getUnsolvedGoals
        unless goals.length == 1 do
          throwError "expression inspection requires exactly one goal after setup"
        goals.head!.withContext do
          let expected? ← spec.expected.mapM fun text =>
            inSnippet spec.id "expected" text do Term.elabType (← parseTerm text)
          let value ← inSnippet spec.id "code" spec.code do
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
    let result ← try
        -- Core counts raw heartbeats, 1,000 times the configured option's units.
        let limit := maxHeartbeats.get (← getOptions)
        withTheReader Core.Context (fun ctx => { ctx with maxHeartbeats := limit * 1000 }) do
          forallBoundedTelescope expected aliases fun _ target => runCase target spec
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
    return Json.mkObj [("result", result), ("messages", toJson messages)]

end MidasProbe
