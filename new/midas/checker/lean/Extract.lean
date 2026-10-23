import Lean
open Lean Elab Command Frontend

namespace Midas.Extract

def name : Name → Json
  | .anonymous => toJson (["anonymous"] : List String)
  | .str p s => Json.arr #[toJson "str", name p, toJson s]
  | .num p n => Json.arr #[toJson "num", name p, toJson n]

partial def findKind (s : Syntax) (kind : Name) : Option Syntax :=
  if s.getKind == kind then some s else s.getArgs.findSome? (findKind · kind)

partial def namePositions (s : Syntax) : Array Nat :=
  if s.getKind == ``Parser.Command.declId then
    #[((s[0].getPos?).getD 0).byteIdx]
  else s.getArgs.flatMap namePositions

partial def hasAtom (s : Syntax) (token : String) : Bool :=
  match s with
  | .atom _ text => text.trimAscii.toString == token
  | _ => s.getArgs.any (hasAtom · token)

def span (s : Syntax) : Json :=
  toJson #[((s.getPos?).getD 0).byteIdx, ((s.getTailPos?).getD 0).byteIdx]

-- Follow only auxiliaries produced by this same command when classifying its
-- own hole. A completed helper referring to an earlier hole is not itself a hole.
partial def ownSorry (env : Environment) (added : NameSet) (e : Expr)
    (seen : NameSet := {}) : Bool :=
  e.hasSorry || e.getUsedConstants.any fun n =>
    if !added.contains n || seen.contains n then false
    else ((env.find? n).bind (·.value? true)).any (fun v => ownSorry env added v (seen.insert n))

partial def scan (imports : Environment) (records : Array Json := #[])
    (external : NameSet := {}) : FrontendM (Array Json × NameSet) := do
  let before ← getCommandState
  let done ← processCommand
  let after ← getCommandState
  if after.messages.hasErrors then
    for message in after.messages.toList do
      IO.eprintln (← message.toString)
    throw (IO.userError "input contains Lean elaboration errors")
  let cmd := (← get).commands.back!
  let added := after.env.constants.map₂.toList.filter (fun (n, _) => !before.env.contains n)
  let addedNames := added.foldl (fun names (n, _) => names.insert n) ({} : NameSet)
  let positions := namePositions cmd
  let valueSyntax := findKind cmd ``Parser.Command.declValSimple
  let simpleKind := cmd.isOfKind ``Parser.Command.declaration &&
    (cmd[1].isOfKind ``Parser.Command.definition || cmd[1].isOfKind ``Parser.Command.theorem)
  let simple := simpleKind && valueSyntax.isSome && added.length == 1 &&
    !hasAtom cmd "where" && !hasAtom cmd "termination_by" && !hasAtom cmd "decreasing_by"
  let mut records := records
  let mut external := external
  let fileMap := (← read).inputCtx.fileMap
  let mut hasAuthored := false
  let freshMessages := after.messages.toList.drop before.messages.toList.length
  let mut hasHole := freshMessages.any (fun message => MessageData.hasTag (· == `hasSorry) message.data)
  -- Examples need not remain in the environment. Preserve an explicit source
  -- hole even if the file disabled its warning.
  if cmd.isOfKind ``Parser.Command.declaration && cmd[1].isOfKind ``Parser.Command.example then
    hasHole := hasHole || hasAtom cmd "sorry" || hasAtom cmd "admit"
  for (n, ci) in added do
    let ranges ← runCommandElabM (findDeclarationRangesCore? n)
    let authored := ranges.any fun r =>
      positions.contains ((fileMap.ofPosition r.selectionRange.pos).byteIdx)
    hasAuthored := hasAuthored || authored
    let value := ci.value? true
    let unfinished := value.any (ownSorry after.env addedNames ·)
    hasHole := hasHole || unfinished
    if ci.type.hasSorry || ci.type.hasMVar || ci.type.hasFVar || value.any Expr.hasSyntheticSorry then
      throw (IO.userError s!"{(privateToUserName n).toString}: incomplete type or recovered elaboration error")
    let typeRefs := ci.type.getUsedConstants
    let valueRefs := (value.map Expr.getUsedConstants).getD #[]
    for ref in typeRefs ++ valueRefs do
      if imports.contains ref then external := external.insert ref
    let .ok signature := Midas.Identity.signature ci
      | throw (IO.userError "unsupported declaration signature")
    let presentation ← if simple && authored then
      runCommandElabM <| liftTermElabM <| Midas.Identity.present ci.type
      else pure ""
    records := records.push <| Json.mkObj [
      ("signature", signature),
      ("presentation", toJson presentation),
      ("selector", toJson (privateToUserName n).toString),
      ("constant", name n),
      ("kind", toJson (if ci.isTheorem then "theorem" else if ci.isDefinition then "def" else "other")),
      ("declaration", span cmd),
      ("replacement", if simple && authored then (valueSyntax.map span).getD Json.null else Json.null),
      ("unfinished", toJson unfinished),
      ("source_authored", toJson authored),
      ("supported", toJson (simple && authored)),
      ("type_references", toJson (typeRefs.map name)),
      ("value_references", toJson (valueRefs.map name))]
  if hasHole && !hasAuthored then
    throw (IO.userError s!"unsupported anonymous or generated hole-containing command at byte {((cmd.getPos?).getD 0).byteIdx}")
  if done then return (records, external)
  scan imports records external

end Midas.Extract

def main (args : List String) : IO UInt32 := do
  let [inputPath, logicalPath, outputPath, heartbeat, recursionDepth] := args
    | throw (IO.userError "expected snapshot source, logical source path, output path, heartbeat and recursion-depth limits")
  let some maxHeartbeat := heartbeat.toNat? | throw (IO.userError "invalid heartbeat limit")
  let some maxRecDepth := recursionDepth.toNat? | throw (IO.userError "invalid recursion-depth limit")
  if maxRecDepth == 0 then throw (IO.userError "recursion-depth limit must be positive")
  unsafe enableInitializersExecution
  initSearchPath (← findSysroot)
  let input ← IO.FS.readFile inputPath
  let ictx := Parser.mkInputContext input logicalPath
  let (header, ps, messages) ← Parser.parseHeader ictx
  let moduleName := ((logicalPath.dropEnd 5).toString.splitOn "/").foldl Name.str Name.anonymous
  let (env, messages) ← processHeader header {} messages ictx (leakEnv := true) (mainModule := moduleName)
  if messages.hasErrors then
    for message in messages.toList do IO.eprintln (← message.toString)
    return 1
  let ((records, external), _) ← ((Midas.Extract.scan env).run {inputCtx := ictx}).run {
    commandState := Command.mkState env messages
      (Lean.maxRecDepth.set (maxHeartbeats.set (({} : Options).setBool `Elab.async false) maxHeartbeat) maxRecDepth),
    parserState := ps, cmdPos := ps.pos }
  let output ← IO.ofExcept (Midas.Binary.encode (Json.mkObj [
    ("format", toJson "midas-source-inventory-v1"), ("declarations", toJson records),
    ("imported_constants", toJson (external.toArray.map Midas.Extract.name))]))
  IO.FS.writeBinFile outputPath output
  return 0
