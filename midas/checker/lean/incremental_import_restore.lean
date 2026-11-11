-- Append-only imports in one admitted package/compiler process.
structure GrowingImports where
  imports : Array Import
  moduleName : Name
  isModule : Bool
  state : ImportState
  env : Environment
initialize growingImports : IO.Ref (Option GrowingImports) ← IO.mkRef none
-- Reuse extension state only when every effective import and loaded part is
-- identical. Otherwise Lean's normal finalizer rebuilds all extension state.
unsafe def sameImportedModules (a b : ImportState) : Bool := Id.run do
  if a.moduleNames != b.moduleNames then return false
  for name in a.moduleNames do
    let some x := a.moduleNameMap[name]? | return false
    let some y := b.moduleNameMap[name]? | return false
    if x.toEffectiveImport.toImport != y.toEffectiveImport.toImport ||
        x.irPhases != y.irPhases || x.hasData != y.hasData ||
        x.needsIRTrans != y.needsIRTrans || x.parts.size != y.parts.size ||
        x.irParts.size != y.irParts.size then return false
    for (left, right) in x.parts.zip y.parts do
      if ptrAddrUnsafe left.2.root != ptrAddrUnsafe right.2.root then return false
    for (left, right) in x.irParts.zip y.irParts do
      if ptrAddrUnsafe left.2.root != ptrAddrUnsafe right.2.root then return false
  return true

def updateDirectImports (env : Environment) (imports : Array Import) : Environment :=
  let env := env.modifyCheckedAsync fun kenv => { kenv with header.imports := imports }
  let env := { env with base.public.header.imports := imports }
  -- Keep the saved realization context in sync, as setMainModule normally does.
  env.setMainModule env.mainModule

open Elab in
def restoreImports (header : Elab.HeaderSyntax) (messages : MessageLog) (ictx : Parser.InputContext)
    (moduleName : Name) : IO (Environment × MessageLog) := do
  if messages.hasErrors then throw (IO.userError "source header parse failed; restart required")
  let imports := header.imports
  let previous ← growingImports.get
  let mut state : ImportState := {}
  if let some old := previous then
    unless old.moduleName == moduleName && old.isModule == header.isModule &&
        old.imports.size ≤ imports.size && imports.extract 0 old.imports.size == old.imports do
      throw (IO.userError "non-append import change; restart required")
    if old.imports == imports then
      let messages := checkDeprecatedImports old.env imports {} ictx header.startPos messages (some header) none
      let messages := checkModuleNamePortability moduleName ictx header.startPos messages
      return (old.env, messages)
    state := old.state
  let level := if header.isModule then OLeanLevel.exported else OLeanLevel.private
  let (env, finishedState) ← withImporting do
    let (_, loadedState) ← (importModulesCore imports level).run state
    let unchanged := previous.any fun (old : GrowingImports) => unsafe sameImportedModules old.state loadedState
    let env ← if unchanged then
        match previous with
        | some old => pure (updateDirectImports old.env imports)
        | none => throw (IO.userError "missing prior import environment")
      else
        finalizeImport loadedState imports {} 0 false true level
    return (env, loadedState)
  let env := env.setMainModule moduleName |>.setModulePackage none
  let messages := checkDeprecatedImports env imports {} ictx header.startPos messages (some header) none
  let messages := checkModuleNamePortability moduleName ictx header.startPos messages
  if messages.hasErrors then throw (IO.userError "import failed; restart required")
  growingImports.set (some { imports, moduleName, isModule := header.isModule, state := finishedState, env })
  return (env, messages)
