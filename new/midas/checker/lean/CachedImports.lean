import Lean
open Lean
namespace Midas.Check

-- Reproduce the source's dependency-first order, not the launcher's import order.
-- Extension initialization can depend on the order of otherwise unrelated imports.
private partial def sourceImportOrder (cache : ImportState) (imports : Array Import) :
    StateT NameSet IO (Array Name) := do
  let mut names := #[]
  for i in imports do
    unless (← get).contains i.module do
      modify (·.insert i.module)
      let some data := cache.moduleNameMap[i.module]?.bind (·.mainModule?)
        | throw (IO.userError s!"missing source import {i.module}")
      names := names ++ (← sourceImportOrder cache data.imports)
      names := names.push i.module
  return names

-- Only the trusted launcher's imported regions are reused, never its declarations
-- or extension state. Lean reconstructs both from the source's import closure.
-- This uses Lean 4.34 internals; module-system headers use processHeader instead.
unsafe def cachedImports (canonical : Environment) (imports : Array Import) : IO Environment := do
  let mut regions : Std.HashMap String (ModuleData × CompactedRegion) := {}
  for region in canonical.header.regions do
    -- finalizeImport owns these ModuleData regions (.olean* and .ir*).
    regions := regions.insert region.filePath.toString (unsafeCast region.root, region)
  let mut cache : ImportState := {}
  for m in canonical.header.modules do
    let path ← findOLean m.module
    let mut parts := #[]
    for suffix in ["", ".server", ".private"] do
      if let some part := regions[path.toString ++ suffix]? then
        parts := parts.push part
    let mut irParts := #[]
    for suffix in ["ir.sig", "ir"] do
      if let some part := regions[(path.withExtension suffix).toString]? then
        irParts := irParts.push part
    -- Reset visibility so each reached module is revisited at the legacy root's
    -- importAll level. Keep hasData: Lean traverses the OLD entry's module data
    -- when upgrading flags; clearing it would skip legacy modules' dependencies.
    let entry : ImportedModule := { m with
      parts, irParts, importAll := false, isExported := false, needsIRTrans := false, irPhases := .runtime }
    cache := { cache with moduleNameMap := cache.moduleNameMap.insert m.module entry,
                          moduleNames := cache.moduleNames.push m.module }
  withImporting do
    let (_, cache) ← (importModulesCore imports).run cache
    -- Entries supplied only by the launcher must not enter the source environment.
    let (names, _) ← (sourceImportOrder cache imports).run {}
    finalizeImport { cache with moduleNames := names } imports {} 0 true true

initialize launchEnvironment : IO.Ref (Option Environment) ← IO.mkRef none

open Elab in
def restoreImports (header : Elab.HeaderSyntax) (messages : MessageLog) (ictx : Parser.InputContext)
    (moduleName : Name) : IO (Environment × MessageLog) := do
  if !(Elab.HeaderSyntax.isModule header) then do
    if let some canonical ← launchEnvironment.get then
      let env ← unsafe cachedImports canonical (Elab.HeaderSyntax.imports header)
      let env := env.setMainModule moduleName
      let messages := checkDeprecatedImports env (Elab.HeaderSyntax.imports header) {} ictx
        (Elab.HeaderSyntax.startPos header) messages (some header) none
      let messages := checkModuleNamePortability moduleName ictx
        (Elab.HeaderSyntax.startPos header) messages
      pure (env, messages)
    else
      processHeader header {} messages ictx (leakEnv := true) (mainModule := moduleName)
  else
    processHeader header {} messages ictx (leakEnv := true) (mainModule := moduleName)

end Midas.Check
