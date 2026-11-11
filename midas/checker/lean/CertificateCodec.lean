import Lean
open Lean
namespace Midas.Binary

def magic : ByteArray := "MIDASDG".toUTF8.push 1

def uint (n : Nat) : ByteArray := Id.run do
  let mut n := n
  let mut out := ByteArray.empty
  repeat
    let b := n % 128
    n := n / 128
    out := out.push (UInt8.ofNat (b + if n > 0 then 128 else 0))
    if n == 0 then break
  return out

structure Node where
  tag : Nat
  text : String := ""
  refs : Array Nat := #[]
  deriving BEq, Hashable, Inhabited

structure Encoder where
  nodes : Array Node := #[]
  intern : Std.HashMap Node Nat := {}
  pointers : Std.HashMap USize (Json × Nat) := {}
  edges : Nat := 0
  heights : Array Nat := #[]
  deriving Inhabited

-- Pointer memoization retains its immutable containers in the cache, preventing
-- allocator address reuse while encoding. Structural interning (not pointer identity) defines the wire.
unsafe def visit (j : Json) (depth : Nat := 0) : StateT Encoder (Except String) Nat := do
  if depth > 128 then throw "certificate graph depth limit exceeded"
  let container := match j with | .arr _ | .obj _ => true | _ => false
  let ptr := ptrAddrUnsafe j
  if container then
    if let some (_, i) := (← get).pointers[ptr]? then
      if depth + (← get).heights[i]! > 128 then throw "certificate graph depth limit exceeded"
      return i
  let node ← match j with
    | .null => pure { tag := 0 }
    | .bool b => pure { tag := if b then 2 else 1 }
    | .num _ => pure { tag := 3, text := toString (← j.getNat?) }
    | .str s => pure { tag := 4, text := s }
    | .arr xs => do
      let refs ← xs.mapM (fun x => visit x (depth+1))
      pure { tag := 5, refs }
    | .obj xs => do
      let mut refs := #[]
      for (k,v) in xs.toList do
        refs := refs.push (← visit (.str k) (depth+1))
        refs := refs.push (← visit v (depth+1))
      pure { tag := 6, refs }
  if node.tag == 3 && node.text.utf8ByteSize > 4096 then throw "oversized graph natural"
  let i ← match (← get).intern[node]? with
    | some i => pure i
    | none => do
      let s ← get
      if s.nodes.size >= 1000000 || s.edges + node.refs.size > 4000000 then
        throw "certificate graph structural limit exceeded"
      let i := s.nodes.size
      let height := node.refs.foldl (fun h r => max h (s.heights[r]! + 1)) 0
      set { s with nodes := s.nodes.push node, intern := s.intern.insert node i,
                   edges := s.edges + node.refs.size, heights := s.heights.push height }
      pure i
  if container then modify fun s => { s with pointers := s.pointers.insert ptr (j, i) }
  return i

partial def visitSlow (j : Json) (depth : Nat := 0) : StateT Encoder (Except String) Nat := do
  if depth > 128 then throw "certificate graph depth limit exceeded"
  let node ← match j with
    | .null => pure { tag := 0 }
    | .bool b => pure { tag := if b then 2 else 1 }
    | .num _ => pure { tag := 3, text := toString (← j.getNat?) }
    | .str s => pure { tag := 4, text := s }
    | .arr xs => do
      let refs ← xs.mapM (fun x => visitSlow x (depth+1))
      pure { tag := 5, refs }
    | .obj xs => do
      let mut refs := #[]
      for (k,v) in xs.toList do
        refs := refs.push (← visitSlow (.str k) (depth+1))
        refs := refs.push (← visitSlow v (depth+1))
      pure { tag := 6, refs }
  if node.tag == 3 && node.text.utf8ByteSize > 4096 then throw "oversized graph natural"
  let i ← match (← get).intern[node]? with
    | some i => pure i
    | none => do
      let s ← get
      if s.nodes.size >= 1000000 || s.edges + node.refs.size > 4000000 then
        throw "certificate graph structural limit exceeded"
      let i := s.nodes.size
      let height := node.refs.foldl (fun h r => max h (s.heights[r]! + 1)) 0
      set { s with nodes := s.nodes.push node, intern := s.intern.insert node i,
                   edges := s.edges + node.refs.size, heights := s.heights.push height }
      pure i
  return i

unsafe def encodeFast (j : Json) : Except String ByteArray := do
  let (root, state) ← (visit j).run {}
  let mut out := magic ++ uint state.nodes.size
  for node in state.nodes do
    out := out.push (UInt8.ofNat node.tag)
    if node.tag == 3 || node.tag == 4 then
      let bytes := node.text.toUTF8
      out := out ++ uint bytes.size ++ bytes
    else if node.tag == 5 || node.tag == 6 then
      out := out ++ uint node.refs.size
      for ref in node.refs do out := out ++ uint ref
    if out.size > 67108864 then throw "certificate graph byte limit exceeded"
  return out ++ uint root

@[implemented_by encodeFast]
def encode (j : Json) : Except String ByteArray := do
  let (root, state) ← (visitSlow j).run {}
  let mut out := magic ++ uint state.nodes.size
  for node in state.nodes do
    out := out.push (UInt8.ofNat node.tag)
    if node.tag == 3 || node.tag == 4 then
      let bytes := node.text.toUTF8
      out := out ++ uint bytes.size ++ bytes
    else if node.tag == 5 || node.tag == 6 then
      out := out ++ uint node.refs.size
      for ref in node.refs do out := out ++ uint ref
    if out.size > 67108864 then throw "certificate graph byte limit exceeded"
  return out ++ uint root

structure Decoder where
  bytes : ByteArray
  pos : Nat := 8
  deriving Inhabited
abbrev Read := StateT Decoder (Except String)

def byte : Read UInt8 := do
  let s ← get
  if s.pos >= s.bytes.size then throw "truncated certificate graph"
  set { s with pos := s.pos+1 }
  return s.bytes[s.pos]!

def readUInt : Read Nat := do
  let mut n := 0
  for shift in [0:5] do
    let b := (← byte).toNat
    n := n + (b % 128) * 128^shift
    if b < 128 then
      if shift > 0 && b == 0 then throw "noncanonical graph integer"
      return n
  throw "oversized graph integer"

def readText : Read String := do
  let size ← readUInt
  let s ← get
  if size > s.bytes.size - s.pos then throw "truncated graph scalar"
  let some text := String.fromUTF8? (s.bytes.extract s.pos (s.pos+size))
    | throw "invalid graph UTF-8"
  set { s with pos := s.pos+size }
  return text

def readGraph : Read Json := do
  let count ← readUInt
  if count == 0 || count > 1000000 then throw "certificate graph node limit exceeded"
  let mut nodes : Array Json := #[]
  let mut depths : Array Nat := #[]
  let mut edges := 0
  for i in [0:count] do
    let tag := (← byte).toNat
    let mut depth := 0
    let value ← match tag with
      | 0 => pure Json.null
      | 1 => pure (Json.bool false)
      | 2 => pure (Json.bool true)
      | 3 => do
        let text ← readText
        if text.utf8ByteSize > 4096 then throw "oversized graph natural"
        let some n := text.toNat? | throw "invalid graph natural"
        if toString n != text then throw "noncanonical graph natural"
        pure (toJson n)
      | 4 => pure (Json.str (← readText))
      | 5 | 6 => do
        let size ← readUInt
        edges := edges + size
        if edges > 4000000 then throw "certificate graph edge limit exceeded"
        if tag == 6 && size % 2 != 0 then throw "invalid graph object"
        let mut xs := #[]
        for _ in [0:size] do
          let ref ← readUInt
          if ref >= i then throw "forward or cyclic graph reference"
          depth := max depth (depths[ref]! + 1)
          xs := xs.push nodes[ref]!
        if tag == 5 then pure (Json.arr xs)
        else do
          let mut pairs := []
          let mut previous : Option String := none
          for k in [0:size/2] do
            let key ← xs[2*k]!.getStr?
            if previous.any (fun p => compare p key != .lt) then
              throw "noncanonical graph object keys"
            previous := some key
            pairs := (key,xs[2*k+1]!) :: pairs
          pure (Json.mkObj pairs.reverse)
      | _ => throw "unknown certificate graph tag"
    if depth > 128 then throw "certificate graph depth limit exceeded"
    nodes := nodes.push value
    depths := depths.push depth
  let root ← readUInt
  if root+1 != count || (← get).pos != (← get).bytes.size then
    throw "invalid graph root or trailing bytes"
  return nodes[root]!

def decode (bytes : ByteArray) : Except String Json := do
  if bytes.size > 67108864 then throw "certificate graph byte limit exceeded"
  if bytes.extract 0 8 != magic then
    throw "expected binary certificate graph"
  let (value, _) ← readGraph.run { bytes }
  if (← encode value) != bytes then throw "noncanonical certificate graph"
  return value
end Midas.Binary
