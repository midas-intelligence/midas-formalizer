@[extern "lean_internal_set_max_memory"]
opaque setCheckerMaxMemory (bytes : USize) : BaseIO Unit

@[extern "midas_isolate"]
opaque isolatedRequest : IO UInt32
@[extern "midas_child_exit"]
opaque exitRequest (code : UInt32) : IO Unit
@[extern "midas_control_open"]
opaque openControl : IO Unit
@[extern "midas_control_write"]
opaque writeControl (line : @& String) : IO Unit

-- Restore only imports in the parent; never elaborate request declarations here.
def preloadRequest (path : String) : IO Unit := do
  let packet ← Midas.Check.readPacket path
  let source ← IO.ofExcept (Midas.Check.str packet "source")
  let logical ← IO.ofExcept (Midas.Check.str packet "logical")
  unsafe enableInitializersExecution
  initSearchPath (← findSysroot)
  let ictx := Parser.mkInputContext source logical
  let (header, _, messages) ← Parser.parseHeader ictx
  let moduleName := ((logical.dropEnd 5).toString.splitOn "/").foldl Name.str Name.anonymous
  let _ ← Midas.Check.restoreImports header messages ictx moduleName

def main (args : List String) : IO UInt32 := do
  if args == ["--preflight"] then return 0
  let [memory, mode, manifestPath] := args
    | throw (IO.userError "expected memory limit, cache mode, and batch manifest")
  unless memory.startsWith "--memory=" do throw (IO.userError "expected memory limit")
  let some mib := (memory.drop 9).toString.toNat? | throw (IO.userError "invalid memory limit")
  unless 0 < mib && mib ≤ 1048576 do throw (IO.userError "invalid memory limit")
  unless mode == "cached" do throw (IO.userError "incremental experiment requires cached mode")
  setCheckerMaxMemory (mib * 1024 * 1024).toUSize
  unless manifestPath == "--stream" do throw (IO.userError "expected streaming mode")
  openControl
  let input ← IO.getStdin
  let mut failed := false
  repeat
    if failed then break
    let line ← input.getLine
    if line.isEmpty then break
    let path := line.trimAscii.toString
    -- Clear request-owned state; the cached import environment is never replaced
    -- with a post-prefix or post-proof environment.
    Midas.Check.contextRows.set {}
    Midas.Check.contextAuxLemmas.set {}
    Midas.Check.sourceAuxLemmas.set none
    Midas.Check.nativeOutputs.set #[]
    Midas.Check.nativeRegions.set #[]
    Midas.Check.launchEnvironment.set none
    let started ← IO.monoNanosNow
    let exitCode ← try
      preloadRequest path
      let forkResult ← isolatedRequest
      if forkResult == 0 then
        let code ← try checkMain [path] catch error => do
          IO.println ("MIDAS_WORKER_ERROR " ++ error.toString)
          pure 2
        (← IO.getStdout).flush
        (← IO.getStderr).flush
        exitRequest code
        pure 2
      else pure (forkResult - 1)
    catch error => do
      IO.println ("MIDAS_WORKER_ERROR " ++ error.toString)
      pure 2
    (← IO.getStdout).flush
    let elapsed := (← IO.monoNanosNow) - started
    writeControl ("MIDAS_WORKER_END " ++ (Json.mkObj [
      ("path", toJson path), ("exit", toJson exitCode.toNat),
      ("nanoseconds", toJson elapsed)]).compress ++ "\n")
    (← IO.getStderr).flush
    if exitCode > 1 then failed := true
  return if failed then 2 else 0
