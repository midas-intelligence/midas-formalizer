-- Match Lean's allocator guard in addition to the runner's RSS/process limits.
@[extern "lean_internal_set_max_memory"]
opaque setCheckerMaxMemory (bytes : USize) : BaseIO Unit

def main (args : List String) : IO UInt32 := do
  if args == ["--preflight"] then return 0
  let [memory, packet] := args | throw (IO.userError "expected memory limit and checker packet")
  unless memory.startsWith "--memory=" do throw (IO.userError "expected memory limit")
  let some mib := (memory.drop 9).toString.toNat? | throw (IO.userError "invalid memory limit")
  unless 0 < mib && mib ≤ 1048576 do throw (IO.userError "invalid memory limit")
  setCheckerMaxMemory (mib * 1024 * 1024).toUSize
  checkMain [packet]
