import Lake
import Lake.Load.Package

open Lean Lake System

-- A flat binary stream, never a JSON rendering of the module inventory.
def writeRow (out : IO.FS.Handle) (total : IO.Ref Nat) (fields : Array String) : IO Unit := do
  let size := fields.foldl (fun n field => n + 8 + field.utf8ByteSize) 1
  total.modify (· + size)
  if (← total.get) > 64*1024*1024 - 1 then throw <| IO.userError "Oversized option inventory"
  out.write (ByteArray.empty.push fields.size.toUInt8)
  for field in fields do
    let bytes := field.toUTF8
    if bytes.size > 16384 then throw <| IO.userError "Oversized option field"
    let mut size := bytes.size
    let mut length := ByteArray.empty
    for _ in [:8] do
      length := length.push (size % 256).toUInt8
      size := size / 256
    out.write length
    out.write bytes

def main (args : List String) : IO Unit := do
  let [request, destination, workspace] := args
    | throw <| IO.userError "Expected request, output and workspace"
  let lean : LeanInstall := {sysroot := ← Lean.findSysroot, githash := Lean.githash}
  let lakeEnv : Lake.Env := { (default : Lake.Env) with lean, lake := LakeInstall.ofLean lean }
  let out ← IO.FS.Handle.mk destination .write
  out.write ("MIDASOPT".toUTF8.push 1)
  let total ← IO.mkRef 9
  let input ← IO.FS.readFile request
  if input.utf8ByteSize > 16*1024*1024 then throw <| IO.userError "Oversized module request"
  let mut package : Option Package := none
  let mut index := 0
  let mut count := 0
  for line in input.splitOn "\n" do
    match line.splitOn "\t" with
    | [""] => pure ()
    | ["P", name, directory, config] =>
      let cfg : LoadConfig := {
        lakeEnv, wsDir := workspace, pkgDir := directory, relPkgDir := directory,
        pkgIdx := index, pkgName := name.toName, relConfigFile := config,
        reconfigure := true, updateDeps := false, updateToolchain := false
      }
      let some pkg ← (loadPackage cfg).toBaseIO
        | throw <| IO.userError s!"Cannot load package {name}"
      package := some pkg
      index := index + 1
      if index > 32 then throw <| IO.userError "Too many packages"
    | ["M", name, source] =>
      let some pkg := package | throw <| IO.userError "Missing package"
      let some mod := pkg.findTargetModule? name.toName
        | throw <| IO.userError s!"No Lake target for {name}"
      if (mod.srcPath "lean").normalize != (FilePath.mk source).normalize then
        throw <| IO.userError s!"Lake source mapping differs for {name}"
      if !mod.leanArgs.isEmpty || !mod.weakLeanArgs.isEmpty then
        throw <| IO.userError s!"Source replay does not admit extra compiler arguments: {name}"
      if !mod.plugins.isEmpty || !mod.dynlibs.isEmpty then
        throw <| IO.userError s!"Source replay does not admit native plugins: {name}"
      writeRow out total #["M", name, pkg.baseName.toString]
      let mut options := 0
      for (key, value) in mod.leanOptions.values do
        options := options + 1
        if options > 64 then throw <| IO.userError "Too many compiler options"
        match value with
        | .ofString text => writeRow out total #["O", key.toString, "s", text]
        | .ofBool flag => writeRow out total #["O", key.toString, "b", if flag then "1" else "0"]
        | .ofNat number => writeRow out total #["O", key.toString, "n", toString number]
      count := count + 1
      if count > 100000 then throw <| IO.userError "Too many modules"
    | _ => throw <| IO.userError "Invalid module request"
  out.write (ByteArray.empty.push 0)
