import Lean

/- Experimental, structural encoding of declaration types and local goals. No repr/pretty
printer, normalization, private-name stripping, or candidate bodies enter it.
Only kernel-irrelevant expression metadata is erased. -/
namespace Midas.Identity
open Lean Elab Command

-- Advisory text travels separately from the exact identity graph. Produce it
-- while the checked type and its declaration context are already in memory.
def present (type : Expr) : MetaM String := do
  withOptions (fun opts => pp.maxSteps.set (opts.setBool `pp.all false |>.setBool `pp.explicit false
      |>.setBool `pp.universes false |>.setBool `pp.fullNames false
      |>.setBool `pp.funBinderTypes true |>.setBool `pp.piBinderTypes true
      |>.setBool `pp.letVarTypes true
      |>.setBool `pp.deepTerms true) 1000000) do
    let text := (← Meta.ppExpr type).pretty
    if text.utf8ByteSize > 1048576 then
      throwError "type presentation exceeds byte limit"
    return text

def node (tag : String) (args : Array Json := #[]) : Json :=
  Json.arr (#[toJson tag] ++ args)

def name : Name → Json
  | .anonymous => node "anonymous"
  | .str p s => node "str" #[name p, toJson s]
  | .num p n => node "num" #[name p, toJson n]

def level : Level → Except String Json
  | .zero => pure (node "zero")
  | .succ u => return node "succ" #[← level u]
  | .max u v => return node "max" #[← level u, ← level v]
  | .imax u v => return node "imax" #[← level u, ← level v]
  | .param n => pure (node "param" #[name n])
  | .mvar _ => .error "unresolved universe metavariable"

def binder : BinderInfo → Json
  | .default => toJson "explicit"
  | .implicit => toJson "implicit"
  | .strictImplicit => toJson "strictImplicit"
  | .instImplicit => toJson "instance"

abbrev ExprCache := Std.HashMap (ExprStructEq × Nat) Json

def exprCached (e : Expr) (depth : Nat := 0) : StateT ExprCache (Except String) Json := do
  if let some value := (← get)[(ExprStructEq.mk e, depth)]? then return value
  let value ← build e depth
  modify fun cache => cache.insert (ExprStructEq.mk e, depth) value
  return value
where
  build (e : Expr) (depth : Nat) : StateT ExprCache (Except String) Json := do
      match e with
      | .bvar i =>
        if i >= depth then throw "loose bound variable"
        return node "bvar" #[toJson i]
      | .fvar _ => throw "open local context: close the telescope first"
      | .mvar _ => throw "unresolved expression metavariable"
      | .sort u => return node "sort" #[← level u]
      | .const n us => return node "const" #[name n, toJson (← us.mapM (fun u => liftM (level u)))]
      | .app f a => return node "app" #[← exprCached f depth, ← exprCached a depth]
      | .lam n t b bi =>
        return node "lam" #[name n, binder bi, ← exprCached t depth, ← exprCached b (depth + 1)]
      | .forallE n t b bi =>
        return node "forall" #[name n, binder bi, ← exprCached t depth, ← exprCached b (depth + 1)]
      | .letE n t v b nd =>
        return node "let" #[name n, ← exprCached t depth, ← exprCached v depth,
          ← exprCached b (depth + 1), toJson nd]
      | .lit (.natVal n) => return node "nat" #[toJson n]
      | .lit (.strVal s) => return node "string" #[toJson s]
      | .proj n i e => return node "proj" #[name n, toJson i, ← exprCached e depth]
      | .mdata _ e => exprCached e depth

def expr (e : Expr) (depth : Nat := 0) : Except String Json := do
  return (← (exprCached e depth).run {}).1

def signature (ci : ConstantInfo) : Except String Json := do
  return Json.mkObj [
    ("target", ← expr ci.type),
    ("universes", toJson (ci.levelParams.map name)),
    ("context", node "closed-telescope")]

def localKind : LocalDeclKind → Json
  | .default => toJson "default"
  | .implDetail => toJson "implementation-detail"
  | .auxDecl => toJson "auxiliary"

-- Encode every local declaration, even unused hypotheses and lets. Do not use
-- mkForallFVars: it can discard lets and generalize nondependent let entries.
-- Fresh FVarIds are replaced with indices into the ordered context. Nothing
-- else (including user names and declaration kinds) is normalized.
def goalSignature (target : Expr) (universes : List Name) : MetaM Json := do
  let mut locals : Array Expr := #[]
  let mut declarations : Array Json := #[]
  for decl in (← getLCtx) do
    let encode (e : Expr) := do
      let e ← instantiateMVars e
      match expr (e.abstract locals) locals.size with
      | .ok j => pure j
      | .error e => throwError "identity: {e}"
    let entry ← match decl with
      | .cdecl _ _ n t bi k => do
        pure (node "local" #[name n, binder bi, localKind k, ← encode t])
      | .ldecl _ _ n t v nd k => do
        pure (node "local-let" #[name n, localKind k, ← encode t, ← encode v, toJson nd])
    declarations := declarations.push entry
    locals := locals.push decl.toExpr
  let encode (e : Expr) := do
    let e ← instantiateMVars e
    match expr (e.abstract locals) locals.size with
    | .ok j => pure j
    | .error e => throwError "identity: {e}"
  let mut instances := #[]
  for inst in (← Meta.getLocalInstances) do
    instances := instances.push (node "local-instance" #[name inst.className, ← encode inst.fvar])
  return Json.mkObj [
    ("target", ← encode target),
    ("universes", toJson (universes.map name)),
    ("context", node "local-context" #[toJson declarations, toJson instances])]

end Midas.Identity
