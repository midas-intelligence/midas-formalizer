import Lean
open Lean Elab Command Meta Frontend
namespace Midas.Check

def allowedAxiom (n : Name) : Bool :=
  n == `propext || n == `Classical.choice || n == `Quot.sound

-- Audit the kernel declaration already enumerated by auditNew. Helpers made
-- inside an asynchronous theorem can be absent from elaborator name lookup.
def auditConstant (ci : ConstantInfo) (assumptions : List Name := []) : CommandElabM Unit := do
  let n := ci.name
  if ci.type.hasMVar || ci.type.hasFVar then throwError "unclosed type: {n}"
  if let some v := (ci.value? true) then
    if v.hasMVar || v.hasFVar then throwError "unclosed value: {n}"
  for ax in (← collectAxioms n) do
    unless allowedAxiom ax || assumptions.contains ax do
      throwError "unapproved axiom: {ax} (from {n})"

def audit (n : Name) : CommandElabM Unit := do
  auditConstant (← getConstInfo n)

-- Tactics can lazily realize equations/congruence lemmas for a definition that
-- predates the submitted fragment. Recreate these in the pre-fragment environment
-- and compare their theorem signatures. Neither the name suffix nor extension
-- state supplied by the fragment is evidence that a declaration is legitimate.
-- The submitted value is still axiom-audited and independently kernel-replayed.
-- Find a trusted lazy declaration, possibly an ancestor of a nested helper.
-- Sparse-case metadata, matcher metadata and reserved names are read only from
-- the environment captured before the submitted fragment.
def generatedHelperRoot? (before : Environment) (name : Name) : Option Name :=
  let matcher := match name with
    | .str parent suffix =>
        ((isEqnReservedNameSuffix suffix || suffix == "splitter") &&
          (privateToUserName? parent).any (isMatcherCore before)) ||
        (Lean.Meta.Match.isCongrEqnReservedNameSuffix suffix && isMatcherCore before parent)
    | _ => false
  let equation := (declFromEqLikeName before name).isSome
  let congruence := match name with
    | .str parent suffix => before.contains parent &&
        (suffix == congrSimpSuffix || isHCongrReservedNameSuffix suffix)
    | _ => false
  let sparseEquation := match name with
    | .str parent "else_eq" => (getSparseCasesOnInfoCore before parent).isSome
    | _ => false
  if equation || congruence || matcher || sparseEquation then some name
  else match name with
    | .str parent _ | .num parent _ => generatedHelperRoot? before parent
    | .anonymous => none

def isGeneratedHelper (before : Environment) (ci : ConstantInfo) : CommandElabM Bool := do
  match ci with
  | .thmInfo _ => pure ()
  | .defnInfo d => unless d.safety == .safe do return false
  | _ => return false
  let some root := generatedHelperRoot? before ci.name | return false
  let saved ← get
  try
    setEnv before
    -- A splitter can generate nested sparse-case helpers. Regenerate its whole
    -- family; a nested helper may also have its own lazy equation action.
    liftCoreM <| executeReservedNameAction root
    unless (← getEnv).contains ci.name do
      liftCoreM <| executeReservedNameAction ci.name
    match ci, (← getEnv).find? ci.name with
    | .thmInfo actual, some (.thmInfo expected) =>
        return actual.levelParams == expected.levelParams && actual.type == expected.type
    | .defnInfo actual, some (.defnInfo expected) =>
        return actual.levelParams == expected.levelParams && actual.type == expected.type &&
          actual.value == expected.value && actual.safety == expected.safety
    | _, _ => return false
  catch _ => return false
  finally set saved

-- Check every new auxiliary as well as the requested declaration, including
-- unused auxiliaries. Only ordinary safe definitions and theorems are admitted.
def auditNew (before : Environment) (expectedName : Name)
    (assumptions : List Name := []) : CommandElabM Unit := do
  let after ← getEnv
  let mut pending := after.constants.map₂.toList.filter (fun (n, _) => !before.contains n)
  let mut replay := before
  for (n, ci) in pending do
    unless before.contains n do
      unless (privateToUserName expectedName).isPrefixOf (privateToUserName n) do
        unless ← isGeneratedHelper before ci do
          throwError "unexpected environment declaration: {n}"
      match ci with
      | .defnInfo d =>
        unless d.safety == .safe do throwError "unsafe/partial declaration: {n}"
      | .thmInfo _ => pure ()
      | _ => throwError "unexpected declaration kind: {n}"
      auditConstant ci assumptions
  while !pending.isEmpty do
    let mut rest := []
    let mut progressed := false
    for (n, ci) in pending do
      let deps := ci.type.getUsedConstants ++ (ci.value! true).getUsedConstants
      if deps.all replay.contains then
        let decl ← match ci with
          | .defnInfo d => pure (Declaration.defnDecl d)
          | .thmInfo t => pure (Declaration.thmDecl t)
          | _ => throwError "unexpected declaration: {n}"
        match replay.addDeclCore 0 10000 decl none true with
        | .ok env => replay := env
        | .error _ => throwError "independent kernel replay rejected: {n}"
        progressed := true
      else rest := (n, ci) :: rest
    unless progressed do throwError "cyclic or undeclared generated dependencies"
    pending := rest


end Midas.Check

namespace Midas.Check

def field (j : Json) (k : String) : Except String Json := j.getObjVal? k
def str (j : Json) (k : String) : Except String String := do (← field j k).getStr?
def nat (j : Json) (k : String) : Except String Nat := do (← field j k).getNat?

partial def readName (j : Json) : Except String Name := do
  match ← j.getArr? with
  | #[.str "anonymous"] => return .anonymous
  | #[.str "str", p, .str s] => return .str (← readName p) s
  | #[.str "num", p, n] => return .num (← readName p) (← n.getNat?)
  | _ => throw "invalid name"

partial def readLevel (j : Json) : Except String Level := do
  match ← j.getArr? with
  | #[.str "zero"] => return .zero
  | #[.str "succ", u] => return .succ (← readLevel u)
  | #[.str "max", u, v] => return .max (← readLevel u) (← readLevel v)
  | #[.str "imax", u, v] => return .imax (← readLevel u) (← readLevel v)
  | #[.str "param", n] => return .param (← readName n)
  | _ => throw "invalid level"

def readBinder : Json → Except String BinderInfo
  | .str "explicit" => pure .default
  | .str "implicit" => pure .implicit
  | .str "strictImplicit" => pure .strictImplicit
  | .str "instance" => pure .instImplicit
  | _ => throw "invalid binder"

unsafe def readExprCached (j : Json) : StateT (Std.HashMap USize (Json × Expr)) (Except String) Expr := do
  let key := ptrAddrUnsafe j
  if let some (_, value) := (← get)[key]? then return value
  let value ← build j
  modify fun cache => cache.insert key (j, value)
  return value
where
  build (j : Json) : StateT (Std.HashMap USize (Json × Expr)) (Except String) Expr := do
      match ← j.getArr? with
      | #[.str "bvar", i] => return .bvar (← i.getNat?)
      | #[.str "sort", u] => return .sort (← readLevel u)
      | #[.str "const", n, us] => return .const (← readName n) (← (← us.getArr?).toList.mapM (fun u => liftM (readLevel u)))
      | #[.str "app", f, a] => return .app (← readExprCached f) (← readExprCached a)
      | #[.str "lam", n, bi, t, b] => return .lam (← readName n) (← readExprCached t) (← readExprCached b) (← readBinder bi)
      | #[.str "forall", n, bi, t, b] => return .forallE (← readName n) (← readExprCached t) (← readExprCached b) (← readBinder bi)
      | #[.str "let", n, t, v, b, nd] => return .letE (← readName n) (← readExprCached t) (← readExprCached v) (← readExprCached b) (← nd.getBool?)
      | #[.str "nat", n] => return .lit (.natVal (← n.getNat?))
      | #[.str "string", .str s] => return .lit (.strVal s)
      | #[.str "proj", n, i, e] => return .proj (← readName n) (← i.getNat?) (← readExprCached e)
      | _ => throw "invalid closed expression"

unsafe def readExprFast (j : Json) : Except String Expr := do
  return (← (readExprCached j).run {}).1

@[implemented_by readExprFast]
partial def readExpr (j : Json) : Except String Expr := do
  match ← j.getArr? with
  | #[.str "bvar", i] => return .bvar (← i.getNat?)
  | #[.str "sort", u] => return .sort (← readLevel u)
  | #[.str "const", n, us] => return .const (← readName n) (← (← us.getArr?).toList.mapM readLevel)
  | #[.str "app", f, a] => return .app (← readExpr f) (← readExpr a)
  | #[.str "lam", n, bi, t, b] => return .lam (← readName n) (← readExpr t) (← readExpr b) (← readBinder bi)
  | #[.str "forall", n, bi, t, b] => return .forallE (← readName n) (← readExpr t) (← readExpr b) (← readBinder bi)
  | #[.str "let", n, t, v, b, nd] => return .letE (← readName n) (← readExpr t) (← readExpr v) (← readExpr b) (← nd.getBool?)
  | #[.str "nat", n] => return .lit (.natVal (← n.getNat?))
  | #[.str "string", .str s] => return .lit (.strVal s)
  | #[.str "proj", n, i, e] => return .proj (← readName n) (← i.getNat?) (← readExpr e)
  | _ => throw "invalid closed expression"

def checked (x : Except String α) : CommandElabM α :=
  match x with
  | .ok a => pure a
  | .error e => throwError "{e}"

def closedContext : Json := toJson (["closed-telescope"] : List String)

def signature (type : Expr) (levels : List Name) (context : Json := closedContext) : CommandElabM Json := do
  return Json.mkObj [("target", ← checked (Midas.Identity.expr type)),
    ("universes", toJson (levels.map Midas.Identity.name)),
    ("context", context)]

def parse {α : Type} (x : Except String α) : TermElabM α :=
  match x with | .ok a => pure a | .error e => throwError "{e}"

def parseTerm (s : String) : CommandElabM (TSyntax `term) := do
  let stx ← checked (Parser.runParserCategory (← getEnv) `term s)
  return ⟨stx.rewriteBottomUp (·.setInfo .none)⟩

-- Context declarations contain terms, never arbitrary command streams. Each is
-- independently audited before any candidate hypotheses are introduced.
initialize contextRows : IO.Ref (Std.HashMap Name Json) ← IO.mkRef {}
initialize contextAuxLemmas : IO.Ref (Std.HashMap Name AuxLemmas) ← IO.mkRef {}
initialize sourceAuxLemmas : IO.Ref (Option AuxLemmas) ← IO.mkRef none

def contextNamespaces (context : Json) : CommandElabM (List Name) := do
  if context == closedContext then return []
  let a ← checked context.getArr?
  unless a.size == 3 && a[0]! == toJson "declaration-context-v2" do
    throwError "unsupported declaration context"
  let rows ← checked a[2]!.getArr?
  rows.toList.mapM fun row => do
    let ns ← checked (str row "namespace")
    unless ns.startsWith "MidasContext_" && ns.length == 77 &&
        (ns.drop 13).toString.toList.all (fun c => c.isDigit || ('a' <= c && c <= 'f')) do
      throwError "invalid context namespace"
    return .str .anonymous ns

def contextConstant (n : Name) : Bool :=
  (privateToUserName n).getRoot.toString.startsWith "MidasContext_"

def auditContextReferences (e : Expr) (namespaces : List Name) : CommandElabM Unit := do
  for n in e.getUsedConstants do
    if contextConstant n && !namespaces.any (·.isPrefixOf (privateToUserName n)) then
      throwError "definition reference outside declared context: {n}"

def withDeclarationContext (context : Json) (action : CommandElabM α) : CommandElabM α := do
  let namespaces ← contextNamespaces context
  withScope (fun s => { s with openDecls := namespaces.reverse.map (OpenDecl.simple · []) ++ s.openDecls }) action

def importDeclarationContext (context : Json) : CommandElabM Unit := do
  let namespaces ← contextNamespaces context
  if namespaces.isEmpty then return
  let rows ← checked ((← checked context.getArr?)[2]!.getArr?)
  -- Preserve the auxiliary layout produced when this context is checked alone.
  -- Each row extends its own prefix's cache, never the last imported sibling's.
  let initial ← match ← sourceAuxLemmas.get with
    | some cache => pure cache
    | none => do
      let cache := auxLemmasExt.getState (← getEnv)
      sourceAuxLemmas.set (some cache)
      pure cache
  let mut cache := initial
  let mut previous : List Name := []
  for row in rows, ns in namespaces do
    let nameText ← checked (str row "name")
    let ident ← parseTerm nameText
    unless ident.raw.isIdent && ident.raw.getId == Name.str .anonymous nameText do
      throwError "invalid context definition name"
    let n := ns ++ ident.raw.getId
    if (← getEnv).contains n then
      unless (← contextRows.get).get? n == some row do throwError "context declaration collision"
      let some saved := (← contextAuxLemmas.get).get? n
        | throwError "missing context auxiliary cache"
      cache := saved
    else
      let before ← getEnv
      let kind ← checked (str row "kind")
      let type ← parseTerm (← checked (str row "type"))
      let value ← parseTerm (← checked (str row "value"))
      withScope (fun s => { s with
          openDecls := previous.reverse.map (OpenDecl.simple · []) ++ s.openDecls,
          varDecls := #[], varUIds := #[], includedVars := [], omittedVars := [],
          opts := (s.opts.setBool `autoImplicit false).setBool `Elab.async false }) do
        let command ← if kind == "def" then
            `(command| noncomputable def $(mkIdent (`_root_ ++ n)) : $type := $value)
          else if kind == "abbrev" then
            `(command| noncomputable abbrev $(mkIdent (`_root_ ++ n)) : $type := $value)
          else throwError "unsupported context declaration kind"
        modifyEnv fun env => auxLemmasExt.setState env cache
        elabCommand command
      auditNew before n
      for (fresh, ci) in (← getEnv).constants.map₂.toList do
        unless before.contains fresh do
          auditContextReferences ci.type (previous ++ [ns])
          auditContextReferences (ci.value! true) (previous ++ [ns])
      if (← get).messages.hasErrors then throwError "context declaration elaboration failed"
      contextRows.modify (·.insert n row)
      cache := auxLemmasExt.getState (← getEnv)
      contextAuxLemmas.modify (·.insert n cache)
    previous := previous ++ [ns]
  modifyEnv fun env => auxLemmasExt.setState env cache

-- Candidate coordinates belong to the exact submitted term, not the source wrapper.
-- Preserve those syntax positions only while using the matching virtual file map.
def withCandidateFragment (label text : String) (action : CommandElabM α) : CommandElabM α := do
  let count := (← get).messages.toList.length
  let emit : CommandElabM Unit := do
    for message in (← get).messages.toList.drop count do
      IO.eprintln (← message.toString)
  withReader (fun ctx => { ctx with fileName := label, fileMap := FileMap.ofString text,
                                    cmdPos := 0, ref := Syntax.missing }) do
    try
      let value ← action
      emit
      return value
    catch error =>
      -- Unlocated infrastructure/wrapper errors must not acquire invented coordinates.
      if let .error ref _ := error then
        if ref.getPos?.isSome then logException error
      emit
      throw error

def parseCandidateFragment (label text : String) : CommandElabM (TSyntax `term) := do
  match Parser.runParserCategory (← getEnv) `term text label with
  | .ok stx => return ⟨stx⟩
  | .error message =>
    -- Parser errors already carry fragment-relative coordinates.
    IO.eprintln message
    throwError "candidate fragment failed to parse"

-- Statements cross the trusted subject boundary. Expand notation from the
-- frozen source, then admit only declarative syntax handled by Lean's ordinary
-- type elaborators. In particular, never execute supplied tactics, quotations,
-- `run_elab`, or custom term elaborators in the replay process. This is an
-- allowlist, not a blacklist of known ways to execute code.
partial def statementSyntax (stx : Syntax) : CommandElabM Syntax := do
  if let some expanded ← liftMacroM (expandMacro? stx) then
    return ← statementSyntax expanded
  if stx.isIdent || stx.isAtom then return stx
  let k := stx.getKind
  if k == ``rawNatLit then
    -- Raw literals must remain Expr.lit, rather than an extra OfNat wrapper:
    -- arithmetic tactics distinguish the two even when definitionally equal.
    unless stx.getNumArgs == 2 && stx[0].getAtomVal == "nat_lit" &&
        stx[1].isNatLit?.isSome do
      throwError "unsupported non-declarative statement syntax: malformed raw natural literal"
  if k == ``Parser.Term.let then
    -- Only ordinary named, nonrecursive definitions. Pattern/equation lets and
    -- elaboration options have separate semantics and are not admitted here.
    let config := stx[1]
    let decl := stx[2]
    let value := decl[0]
    unless stx.getNumArgs == 5 && config.getKind == ``Parser.Term.letConfig &&
        config.getNumArgs == 1 && config[0].getKind == nullKind &&
        config[0].getNumArgs == 0 && decl.getKind == ``Parser.Term.letDecl &&
        decl.getNumArgs == 1 && value.getKind == ``Parser.Term.letIdDecl &&
        value.getNumArgs == 5 && value[0].getKind == ``Parser.Term.letId &&
        value[0].getNumArgs == 1 && value[0][0].isIdent do
      throwError "unsupported non-declarative statement syntax: only ordinary named let bindings are supported"
  unless [nullKind, `hygieneInfo, `num, `str, `fieldIdx, ``rawNatLit,
      ``Parser.Term.app, ``Parser.Term.explicit, ``Parser.Term.explicitUniv,
      ``Parser.Term.forall, ``Parser.Term.arrow, ``Parser.Term.depArrow, ``Parser.Term.fun,
      ``Parser.Term.basicFun, ``Parser.Term.explicitBinder,
      ``Parser.Term.implicitBinder, ``Parser.Term.strictImplicitBinder,
      ``Parser.Term.instBinder, ``Parser.Term.typeAscription,
      ``Parser.Term.proj, ``Parser.Term.namedArgument,
      ``Parser.Term.let, ``Parser.Term.letConfig, ``Parser.Term.letDecl,
      ``Parser.Term.letIdDecl, ``Parser.Term.letId, ``Parser.Term.typeSpec,
      ``Parser.Term.type, ``Parser.Term.sort, ``Parser.Term.prop,
      -- Arithmetic notation expands to these built-in elaborators, not `app`.
      -- Recursively validate operands below, including any nested macros.
      ``Parser.Term.binop, ``Parser.Term.unop,
      ``Parser.Term.leftact, ``Parser.Term.rightact,
      ``Parser.Term.binrel, ``Parser.Term.hole,
      ``Parser.Term.paren, ``Parser.Term.hygienicLParen].contains k ||
      (`Lean.Parser.Level).isPrefixOf k do
    throwError "unsupported executable or non-declarative statement syntax: {k}"
  return stx.setArgs (← stx.getArgs.mapM statementSyntax)

def encodeDecl (ci : ConstantInfo) : CommandElabM Json := do
  let kind ← match ci with
    | .thmInfo _ => pure "theorem"
    | .defnInfo d => if d.safety == .safe then pure "def" else throwError "unsafe declaration"
    | _ => throwError "unsupported certificate declaration"
  return Json.mkObj [("name", Midas.Identity.name ci.name), ("kind", toJson kind),
    ("signature", ← signature ci.type ci.levelParams),
    ("value", ← liftTermElabM do return toJson (← ppExpr (ci.value! true)).pretty)]


def substitute (e : Expr) (peers : Array (Name × List Level × Expr)) : CommandElabM Expr := do
  for (n, us, _) in peers do
    for c in e.getUsedConstants do
      if c == n then
        let bad := e.find? fun t => match t with
          | .const m vs => m == n && vs != us
          | _ => false
        if bad.isSome then throwError "unsupported polymorphic peer instantiation"
  return e.replace fun t => match t with
    | .const n us => (peers.find? (fun (m, vs, _) => m == n && vs == us)).map (·.2.2)
    | _ => none

partial def abstractType (deps : List Json) (target : Expr) (levels : List Name)
    (xs : Array Expr := #[])
    (peers : Array (Name × List Level × Expr) := #[]) : TermElabM Expr := do
  match deps with
  | [] =>
    let target ← liftCommandElabM <| substitute target peers
    mkForallFVars xs target (usedOnly := false)
  | dep :: rest =>
    let parameter ← parse (readName (← parse (field dep "parameter")))
    let iface ← parse (field dep "interface")
    let sig ← parse (field iface "signature")
    let us ← parse ((← parse ((← parse (field sig "universes")).getArr?)).toList.mapM readName)
    unless us.all levels.contains do throwError "dependency introduces unbound universes"
    let raw ← parse (readExpr (← parse (field sig "target")))
    let ty ← liftCommandElabM <| substitute raw peers
    discard <| inferType ty
    withLocalDeclD parameter ty fun x => do
      let peers ← if (← parse (str dep "origin")) == "frozen_peer" then do
        let n ← parse (readName (← parse (field iface "constant")))
        pure (peers.push (n, us.map Level.param, x))
      else pure peers
      abstractType rest target levels (xs.push x) peers

-- Conditional assumptions are ordinary universe-polymorphic declarations.
-- Only the exact interfaces in the controller's packet authorize these names;
-- they are never admitted as axioms of a completed proof.
def lemmaAssumptions (deps : Array Json) : CommandElabM Json := do
  return toJson (← deps.mapM fun dep => do
    unless (← checked (str dep "origin")) == "abstracted_lemma" do
      throwError "polymorphic assumptions require abstracted lemmas"
    return Json.mkObj [("parameter", ← checked (field dep "parameter")),
      ("signature", ← checked (field (← checked (field dep "interface")) "signature"))])

def installAssumptions (assumptions : Json) : CommandElabM (List Name) := do
  (← checked assumptions.getArr?).toList.mapM fun dep => do
    let n ← checked (readName (← checked (field dep "parameter")))
    if n.isAnonymous || (← getEnv).contains n ||
        (`MidasCandidateResult).isPrefixOf n then
      throwError "dependency alias collides with checker or frozen declaration"
    let sig ← checked (field dep "signature")
    let type ← checked (readExpr (← checked (field sig "target")))
    let us ← checked ((← checked ((← checked (field sig "universes")).getArr?)).toList.mapM readName)
    liftCoreM <| addDecl (.axiomDecl { name := n, levelParams := us, type, isUnsafe := false })
    return n

-- Only the original, immutable source prefix supplies executable declarations
-- to the replay process; submitted statements have restricted declarative syntax.
-- The selected declaration is temporarily elaborated for its structural type,
-- then discarded before candidate elaboration or certificate loading.
partial def seek (offset : Nat) : FrontendM Command.State := do
  let before ← getCommandState
  let done ← processCommand
  let cmd := (← get).commands.back!
  if (← getCommandState).messages.hasErrors then
    throw (IO.userError "source prefix failed to elaborate")
  if ((cmd.getPos?).getD 0).byteIdx == offset then
    return before
  if done || ((cmd.getPos?).getD 0).byteIdx > offset then
    throw (IO.userError "target boundary not found")
  seek offset

end Midas.Check

namespace Midas.Check

-- Lookup reads only the restored prefix. Query strings are parsed as identifiers,
-- never elaborated as terms or commands; no submitted tactic can execute here.
def lookupSourceRange (n : Name) : CommandElabM Json := do
  let some ranges ← findDeclarationRanges? n | return Json.null
  let module ← findModuleOf? n
  return Json.mkObj [
    ("module", module.map (toJson ·.toString) |>.getD Json.null),
    ("start", toJson #[ranges.range.pos.line, ranges.range.pos.column]),
    ("end", toJson #[ranges.range.endPos.line, ranges.range.endPos.column])]

def lookup (packet : Json) : CommandElabM Json := do
  let queries ← checked ((← checked (field packet "names")).getArr?)
  unless queries.size > 0 && queries.size ≤ 16 do throwError "lookup name allowance exceeded"
  let body ← checked ((← checked (field packet "include_body")).getBool?)
  let sourceBody := (packet.getObjValAs? String "body_format").toOption == some "source"
  let mut rows := #[]
  for query in queries do
    let text ← checked query.getStr?
    unless text.utf8ByteSize > 0 && text.utf8ByteSize ≤ 1024 do throwError "lookup name size exceeded"
    let mut sourceRange := Json.null
    let row ← try
      let stx ← parseTerm text
      unless stx.raw.isIdent do throwError "lookup requires an exact identifier"
      let n := stx.raw.getId.eraseMacroScopes
      let aliases ← checked ((← checked (field packet "lookup_aliases")).getArr?)
      if let some dep := aliases.find? (fun d => (d.getObjVal? "parameter").toOption == some (Midas.Identity.name n)) then
        if (← getEnv).contains n then throwError "dependency alias collides with frozen declaration"
        let sig ← checked (field dep "signature")
        let ty ← checked (readExpr (← checked (field sig "target")))
        let pretty ← liftTermElabM do return (← ppExpr ty).pretty
        pure <| Json.mkObj [("query", query), ("status", toJson "available"),
          ("constant", Midas.Identity.name n), ("kind", toJson "parameter"),
          ("type", toJson pretty), ("body", Json.null), ("error", Json.null)]
      else
        let some ci := (← getEnv).find? n | throwError "declaration is not visible in frozen prefix"
        let kind ← match ci with
          | .thmInfo _ => pure "theorem"
          | .defnInfo d =>
            unless d.safety == .safe do throwError "unsafe or partial definition"
            pure "def"
          | .inductInfo _ => pure "inductive"
          | .ctorInfo _ => pure "constructor"
          | .recInfo _ => pure "recursor"
          | _ => throwError "unsupported lookup declaration kind"
        auditConstant ci
        if body && sourceBody then
          sourceRange ← lookupSourceRange n
        let value ← if body && !sourceBody && kind == "def" then
          liftTermElabM do return toJson (← ppExpr (ci.value! true)).pretty
        else pure Json.null
        let pretty ← liftTermElabM do return (← ppExpr ci.type).pretty
        pure <| Json.mkObj [("query", query), ("status", toJson "available"),
          ("constant", Midas.Identity.name n), ("kind", toJson kind),
          ("type", toJson pretty), ("body", value), ("error", Json.null)]
    catch error =>
      let message ← error.toMessageData.toString
      sourceRange := Json.null
      pure <| Json.mkObj [("query", query), ("status", toJson "unavailable"),
        ("constant", Json.null), ("kind", Json.null),
        ("type", Json.null), ("body", Json.null), ("error", toJson message)]
    let row := if sourceBody then row.setObjVal! "source_range" sourceRange else row
    rows := rows.push row
  return Json.mkObj [("format", toJson (if sourceBody then "midas-lookup-result-source-v1" else "midas-lookup-result-context-v1")),
    ("request", ← checked (field packet "request")), ("records", toJson rows)]

-- Retain the submitted tactic source. The kernel audit below checks all generated
-- declarations, but no proof expression crosses the process boundary.
def elaborateProofSource (root : Name) (levels : List Name) (expected : Expr)
    (text : String) : CommandElabM Unit := do
  withCandidateFragment "candidate-proof.lean" text do
    let body ← parseCandidateFragment "candidate-proof.lean" text
    let scope ← getScope
    liftTermElabM do
      let type ← Term.exprToSyntax expected
      let us := levels.toArray.map mkIdent
      let view := mkDefViewOfDef { computeKind := .noncomputable }
        (← `(Parser.Command.definition| def
          $(mkIdent (`_root_ ++ root)).{$us,*} : $type := $body))
      Term.withLevelNames [] <|
        Term.elabMutualDef #[] { scope with levelNames := [] } #[view]

-- Native artifacts contain only declarations, never import/extension state.
initialize nativeRegions : IO.Ref (Array CompactedRegion) ← IO.mkRef #[]
initialize nativeOutputs : IO.Ref (Array (String × ByteArray)) ← IO.mkRef #[]

def cacheModuleName (id : String) : Name := .str .anonymous ("MidasDecl_" ++ id)

def renameNative (constants : Array ConstantInfo) (source target : Name) : CommandElabM (Array ConstantInfo) := do
  let names := constants.map fun ci =>
    let user := privateToUserName ci.name
    (ci.name, if source.isPrefixOf user then user.replacePrefix source target else ci.name)
  let rename := fun n => ((names.find? (fun pair => pair.1 == n)).map (·.2)).getD n
  let expr := fun e : Expr => e.replace fun t => match t with
    | .const n us => some (.const (rename n) us)
    | _ => none
  constants.mapM fun ci => match ci with
    | .defnInfo d => pure (.defnInfo { d with
        name := rename d.name
        type := expr d.type, value := expr d.value, all := d.all.map rename })
    | .thmInfo d => pure (.thmInfo { d with
        name := rename d.name
        type := expr d.type, value := expr d.value, all := d.all.map rename })
    | _ => throwError "unexpected native declaration kind"

def saveNative (id : String) (before : Environment) (root : Name) : CommandElabM Unit := do
  -- Disposable cache output must not turn a checked proof into a resource
  -- failure. Bound the entire batch, independently of proof/dependency count.
  let outputs ← nativeOutputs.get
  let used := outputs.foldl (fun total (_, bytes) => total + bytes.size) 0
  if outputs.size >= 256 || used >= 8388608 then return
  let constants := ((← getEnv).constants.map₂.toList.filter
    (fun (n, _) => !before.contains n)).toArray.map (·.2)
  let constants ← renameNative constants root `MidasCandidateResult
  let data : ModuleData := {
    isModule := false, imports := #[], constants
    constNames := constants.map (·.name), extraConstNames := #[], entries := #[] }
  let file : System.FilePath := "/tmp/midas-native-declarations-" ++ toString (← IO.Process.getPID) ++ ".olean"
  -- The proof was audited before this optional serialization. In particular,
  -- scratch-space exhaustion while saving a cache is not a proof rejection.
  try
    saveModuleData file (cacheModuleName id) data
    let bytes ← IO.FS.readBinFile file
    if used + bytes.size <= 8388608 then
      nativeOutputs.modify (·.push (id, bytes))
  catch _ => pure ()
  try IO.FS.removeFile file catch _ => pure ()

def replayNative (file : System.FilePath) (root : Name) (helperBase : Environment) : CommandElabM Unit := do
  let (data, region) ← readModuleData file
  nativeRegions.modify (·.push region)
  unless !data.isModule && data.imports.isEmpty && data.entries.isEmpty && data.extraConstNames.isEmpty do
    throwError "native artifact contains imports or extension state"
  unless data.constNames == data.constants.map (·.name) do throwError "native declaration names mismatch"
  unless 0 < data.constants.size && data.constants.size ≤ 4096 do throwError "native declaration count exceeded"
  let constants ← renameNative data.constants `MidasCandidateResult root
  let mut pending := constants.toList
  let mut seen : NameSet := {}
  for ci in pending do
    if seen.contains ci.name then throwError "native artifact duplicates declaration"
    if let some old := (← getEnv).find? ci.name then
      unless (← isGeneratedHelper helperBase ci) && old.type == ci.type &&
          old.levelParams == ci.levelParams && old.value? true == ci.value? true do
        throwError "native artifact redeclares context"
    seen := seen.insert ci.name
    match ci with
    | .defnInfo d => unless d.safety == .safe do throwError "unsafe native declaration"
    | .thmInfo _ => pure ()
    | _ => throwError "unexpected native declaration kind"
  while !pending.isEmpty do
    let mut rest := []
    let mut progress := false
    for ci in pending do
      if (← getEnv).contains ci.name then
        progress := true
      else if (ci.type.getUsedConstants ++ (ci.value! true).getUsedConstants).all (← getEnv).contains then
        let decl ← match ci with
          | .defnInfo d => pure (Declaration.defnDecl d)
          | .thmInfo d => pure (Declaration.thmDecl d)
          | _ => throwError "unexpected native declaration kind"
        match (← getEnv).addDeclCore 0 10000 decl none true with
        | .ok env => setEnv env
        | .error _ => throwError "native artifact kernel rejected"
        progress := true
      else rest := ci :: rest
    unless progress do throwError "native artifact has cyclic or missing dependencies"
    pending := rest

-- Missing artifacts are rebuilt from durable source and independently audited.
-- Present artifacts never execute their source or import elaborator extensions.
def replayNativeDeclarations (spec : Json) (assumptions : List Name := []) : CommandElabM Unit := do
  let nodes ← checked ((← checked (field spec "nodes")).getArr?)
  -- Native files restore constants without extension state. Validate shared
  -- lazy helpers against the untouched source environment: an earlier node's
  -- splitter constant can otherwise prevent Lean from realizing its metadata.
  let helperBase ← getEnv
  for node in nodes do
    let id ← checked (str node "id")
    let d ← checked (field node "declaration")
    let n ← checked (readName (← checked (field d "name")))
    unless (`MidasCandidateResult).isPrefixOf n do throwError "native root outside reserved namespace"
    if (← getEnv).contains n then throwError "native root redeclares context"
    let sig ← checked (field d "signature")
    let context ← checked (field sig "context")
    importDeclarationContext context
    let base ← getEnv
    let ty ← checked (readExpr (← checked (field sig "target")))
    let us ← checked ((← checked ((← checked (field sig "universes")).getArr?)).toList.mapM readName)
    let file ← try pure (some (← findOLean (cacheModuleName id))) catch _ => pure none
    if let some file := file then
      replayNative file n helperBase
    else
      withDeclarationContext context <| elaborateProofSource n us ty (← checked (str d "source"))
    let ci ← getConstInfo n
    unless ci.levelParams == us do throwError "native declaration universes changed"
    liftTermElabM do
      unless ← isDefEq ci.type ty do throwError "native declaration type mismatch"
    auditNew base n assumptions
    if (← get).messages.hasErrors then throwError "native declaration elaboration failed"
    if file.isNone then saveNative id base n
  let root ← checked (readName (← checked (field spec "root")))
  let ci ← getConstInfo root
  liftCoreM <| addDecl (.defnDecl {
    name := `MidasCandidateResult, levelParams := ci.levelParams, type := ci.type,
    value := .const root (ci.levelParams.map .param), hints := .abbrev, safety := .safe })

-- Elaboration is confined to the producer. Inline generated auxiliaries so the
-- resulting closed type depends only on the independently restored prefix.
-- The verifier never imports producer extensions or executes statement syntax.
def closeStatement (before : Environment) (target : Expr)
    (header : Name := .anonymous) : CommandElabM Expr := do
  let after ← getEnv
  for (n, ci) in after.constants.map₂.toList do
    unless before.contains n || n == header do auditConstant ci
  liftCoreM <| Core.transform target (pre := fun e => do
    if let .const n us := e then
      unless before.contains n do
        let ci ← getConstInfo n
        match ci with
        | .defnInfo d => unless d.safety == .safe do throwError "unsafe statement auxiliary: {n}"
        | .thmInfo _ => pure ()
        | _ => throwError "unsupported statement auxiliary: {n}"
        return .visit (ci.value! true |>.instantiateLevelParams ci.levelParams us)
    return .continue)

def check (packet : Json) (original : Environment) : CommandElabM Json := do
  let imports := (packet.getObjVal? "imports").toOption.getD (toJson (#[] : Array String))
  let attachImports := fun (result : Json) => if imports == toJson (#[] : Array String) then result
    else result.setObjVal! "imports" imports
  if (packet.getObjValAs? String "mode").toOption == some "replay" then
    let cert ← checked (field packet "certificate")
    unless (cert.getObjVal? "imports").toOption.getD (toJson (#[] : Array String)) == imports do
      throwError "certificate import context mismatch"
  let n ← checked (readName (← checked (field packet "constant")))
  let some ci := original.find? n | throwError "missing selected target"
  let actual ← signature ci.type ci.levelParams
  unless actual == (← checked (field packet "signature")) do throwError "frozen target signature mismatch"
  let helperSig := (packet.getObjVal? "helper_signature").toOption
  let helperDeps := ((packet.getObjVal? "helper_dependencies").bind Json.getArr?).toOption.getD #[]
  let helperPolymorphic := helperDeps.all fun dep =>
    (dep.getObjValAs? String "origin").toOption == some "abstracted_lemma"
  let helperTarget ← match helperSig with
    | none => pure ci.type
    | some sig => do
      -- The checked signature is a closed telescope. Source section variables
      -- cannot become extra assumptions in the helper's local context.
      modifyScope fun s => { s with
        varDecls := #[], varUIds := #[], includedVars := [], omittedVars := [],
        opts := (s.opts.setBool `autoImplicit false).setBool `Elab.async false }
      let context ← checked (field sig "context")
      importDeclarationContext context
      for dep in helperDeps do
        let sig ← checked (field (← checked (field dep "interface")) "signature")
        importDeclarationContext (← checked (field sig "context"))
      let namespaces ← contextNamespaces context
      modifyScope fun s => { s with openDecls := namespaces.reverse.map (OpenDecl.simple · []) ++ s.openDecls }
      let target ← checked (readExpr (← checked (field sig "target")))
      let levels ← checked ((← checked ((← checked (field sig "universes")).getArr?)).toList.mapM readName)
      modifyScope fun s => { s with levelNames := levels }
      for dep in helperDeps do
        let alias ← checked (readName (← checked (field dep "parameter")))
        if (← getEnv).contains alias then throwError "dependency alias collides with frozen declaration"
      if helperPolymorphic then
        let _ ← installAssumptions (← lemmaAssumptions helperDeps)
        pure target
      else liftTermElabM <| abstractType helperDeps.toList target levels
  if (← checked (str packet "mode")) == "probe" then
    if helperSig.isNone then modifyScope fun s => { s with levelNames := ci.levelParams }
    let spec ← checked (fromJson? (α := MidasProbe.Case) (← checked (field packet "case")))
    let row ← MidasProbe.run helperTarget spec (if helperPolymorphic then 0 else helperDeps.size)
    return Json.mkObj [("format", toJson "midas-probe-case-v1"),
      ("request", ← checked (field packet "request")), ("row", row)]
  -- No section local is silently promoted to a hypothesis. The frozen type is
  -- already a closed telescope and the candidate supplies that whole value.
  modifyScope fun s => { s with
    varDecls := #[], varUIds := #[], includedVars := []
    omittedVars := [],
    opts := (s.opts.setBool `autoImplicit false).setBool `Elab.async false }
  let mode ← checked (str packet "mode")
  if mode == "names" then
    let raw ← checked ((← checked (field packet "parameters")).getArr?)
    let mut names : Array Name := #[]
    for item in raw do
      let text ← checked item.getStr?
      let stx ← parseTerm text
      unless stx.raw.isIdent do throwError "dependency parameter must be an identifier"
      let parameter := stx.raw.getId
      if parameter.isAnonymous || names.contains parameter then
        throwError "anonymous or duplicate dependency parameter"
      names := names.push parameter
    return Json.mkObj [("format", toJson "midas-parameter-names-v1"),
      ("request", ← checked (field packet "request")),
      ("parameters", toJson (names.map Midas.Identity.name))]
  let workContext := (packet.getObjVal? "declaration_context").toOption.getD closedContext
  let statementContext := (packet.getObjVal? "statement_context").toOption.getD closedContext
  importDeclarationContext statementContext
  importDeclarationContext workContext
  for dep in (← checked ((← checked (field packet "dependencies")).getArr?)) do
    let sig ← checked (field (← checked (field dep "interface")) "signature")
    importDeclarationContext (← checked (field sig "context"))
  if mode == "replay" then
    let cert ← checked (field packet "certificate")
    if (cert.getObjValAs? String "format").toOption == some "midas-source-proof-v1" then
      for d in (← checked ((← checked (field cert "declarations")).getArr?)) do
        importDeclarationContext (← checked (field (← checked (field d "signature")) "context"))
  let before ← getEnv
  let hasStatement := (← checked (field packet "statement")) != Json.null
  let elaborated := (packet.getObjValAs? String "statement_protocol").toOption == some "elaborated-statements-v1"
  let bound := (packet.getObjVal? "statement_interface").toOption
  let statementLevels ← IO.mkRef (none : Option (List Name))
  let suppliedSig ← if let some binding := bound then
      if !hasStatement then throwError "statement interface without statement"
      pure (some (← checked (field binding "signature")))
    else if elaborated && mode == "replay" then do
      let cert ← checked (field packet "certificate")
      unless (← checked (str packet "purpose")) == "interface" &&
        (← checked (str cert "format")) == "midas-elaborated-interface-v1" &&
        (← checked (field cert "statement")) == (← checked (field packet "statement")) &&
        (← checked (field cert "request")) == (← checked (field packet "request")) do
        throwError "elaborated statement binding mismatch"
      pure (some (← checked (field cert "signature")))
    else pure none
  if elaborated && (← checked (str packet "purpose")) != "interface" then
    throwError "statement elaboration requires a separate interface phase"
  let target ← if !hasStatement then pure ci.type else
    if let some sig := suppliedSig then do
      unless (← checked (field sig "context")) == statementContext do
        throwError "statement context mismatch"
      checked (readExpr (← checked (field sig "target")))
    else do
      let text ← checked (str packet "statement")
      withDeclarationContext statementContext <| withCandidateFragment "candidate-statement.lean" text do
        let parsed ← parseCandidateFragment "candidate-statement.lean" text
        let stx ← if elaborated then pure parsed.raw else statementSyntax parsed
        if !elaborated then
          -- Historical expression-only packets retain their exact telescope
          -- and elaboration layout. New bindings use declaration elaboration.
          liftTermElabM <| Term.withDeclName `MidasCandidateResult do
            let e ← Term.elabType stx
            Term.synthesizeSyntheticMVarsNoPostponing
            instantiateMVars e
        else do
          -- Use Lean's declaration header elaborator, including ordinary universe
          -- generalization. This temporary axiom certifies no proof and is removed
          -- before the interface value is independently kernel-checked.
          let header := `MidasCandidateStatement
          if (← getEnv).contains header then throwError "reserved statement declaration exists"
          let typeSyntax : Term := ⟨stx⟩
          let declarationName := mkIdent (`_root_ ++ header)
          elabCommand (← `(command| axiom $declarationName:ident : $typeSyntax))
          let info ← getConstInfo header
          statementLevels.set (some info.levelParams)
          let e ← closeStatement before info.type header
          setEnv before
          pure e
  let sourceLevels ← getLevelNames
  let inferredLevels ← statementLevels.get
  let levels ← if let some sig := suppliedSig then
      checked ((← checked ((← checked (field sig "universes")).getArr?)).toList.mapM readName)
    else pure (if !hasStatement then ci.levelParams else
      inferredLevels.getD (sourceLevels.filter ((collectLevelParams {} target).params.contains ·)))
  if elaborated then
    if (← get).messages.hasErrors then throwError "statement elaboration reported errors"
    setEnv before
  modifyScope fun s => { s with levelNames := levels }
  -- New identity envelopes cannot describe the values of generated type
  -- dependencies. Admit only constants already in the frozen source context.
  for dep in target.getUsedConstants do
    unless before.contains dep do
      throwError "generated or unavailable type dependency is unsupported: {dep}"
  if hasStatement then auditContextReferences target (← contextNamespaces statementContext)
  let sig ← signature target levels statementContext
  let deps ← checked ((← checked (field packet "dependencies")).getArr?)
  let root := `MidasCandidateResult
  if before.contains root then throwError "reserved checker declaration exists"
  let purpose ← checked (str packet "purpose")
  unless purpose == "proof" || purpose == "interface" do throwError "unknown checking purpose"
  let polymorphic := purpose == "proof" &&
    (packet.getObjValAs? String "dependency_protocol").toOption == some "polymorphic-lemmas-v1"
  let assumptions ← if polymorphic then lemmaAssumptions deps else pure Json.null
  if mode == "replay" then
    let saved := ((← checked (field packet "certificate")).getObjVal? "assumptions").toOption.getD Json.null
    unless saved == assumptions do throwError "conditional assumption manifest mismatch"
  let assumptionNames ← if polymorphic then installAssumptions assumptions else pure []
  let proofBefore ← getEnv
  let abstracted ← if polymorphic then pure target else
    liftTermElabM <| Term.withDeclName root <| abstractType deps.toList target levels
  let expected ← if purpose == "interface" then
    liftTermElabM do
      let type ← whnf (← inferType abstracted)
      unless type.isSort do throwError "statement expression is not a type"
      pure type
  else pure abstracted
  let mut proofSource := ""
  if mode == "produce" then
    if purpose == "interface" then
      liftCoreM <| addDecl (.defnDecl {
        name := root, levelParams := levels
        type := expected, value := abstracted, hints := .abbrev, safety := .safe})
    else
      proofSource ← checked (str packet "candidate")
      withDeclarationContext workContext <| elaborateProofSource root levels expected proofSource
  else if mode == "replay" then
    let cert ← checked (field packet "certificate")
    unless (← checked (field cert "request")) == (← checked (field packet "request")) &&
      (← checked (str cert "purpose")) == purpose do throwError "source proof binding mismatch"
    unless (← checked (field cert "signature")) == sig do throwError "source proof subject mismatch"
    if purpose == "interface" then
      liftCoreM <| addDecl (.defnDecl {
        name := root, levelParams := levels
        type := expected, value := abstracted, hints := .abbrev, safety := .safe })
    else
      unless (← checked (str cert "format")) == "midas-source-proof-v1" do
        throwError "replay requires source proof"
      if let .ok file := packet.getObjValAs? String "native_proof" then
        replayNative (← findOLean (.str .anonymous file)) root proofBefore
      else if let .ok spec := packet.getObjVal? "native_declarations" then
        replayNativeDeclarations spec assumptionNames
      else
        for j in (← checked ((← checked (field cert "declarations")).getArr?)) do
          let declName ← checked (readName (← checked (field j "name")))
          if (← getEnv).contains declName then throwError "source proof redeclares context"
          unless root.isPrefixOf declName do throwError "source proof declaration outside reserved namespace"
          let sig ← checked (field j "signature")
          let ty ← checked (readExpr (← checked (field sig "target")))
          let us ← checked ((← checked ((← checked (field sig "universes")).getArr?)).toList.mapM readName)
          for dep in (← checked ((← checked (field j "requires")).getArr?)) do
            unless (← getEnv).contains (← checked (readName dep)) do throwError "missing source dependency"
          withDeclarationContext (← checked (field sig "context")) <|
            elaborateProofSource declName us ty (← checked (str j "source"))
  else throwError "unsupported checker mode"
  let result ← getConstInfo root
  unless result.levelParams == levels do throwError "candidate universes changed"
  liftTermElabM do
    unless ← isDefEq result.type expected do throwError "candidate type mismatch"
  if purpose == "interface" then
    liftTermElabM do
      unless ← isDefEq (result.value! true) abstracted do throwError "interface value mismatch"
  auditNew proofBefore root assumptionNames
  if (← get).messages.hasErrors then throwError "candidate elaboration reported errors"
  if elaborated then
    return attachImports <| Json.mkObj [("format", toJson "midas-elaborated-interface-v1"),
      ("request", ← checked (field packet "request")), ("purpose", toJson "interface"),
      ("statement", ← checked (field packet "statement")), ("signature", sig)]
  if purpose == "proof" then
    if mode == "replay" then
      if let .ok receipt := packet.getObjVal? "replay_receipt" then
        unless (← checked (str receipt "format")) == "midas-proof-replay-receipt-v1" &&
            (← checked (field receipt "request")) == (← checked (field packet "request")) do
          throwError "invalid replay receipt binding"
        let digest ← checked (str receipt "certificate_sha256")
        unless digest.length == 64 && digest.toList.all (fun c =>
            ('0' <= c && c <= '9') || ('a' <= c && c <= 'f')) do
          throwError "invalid replay certificate digest"
        return receipt
      let proof ← checked (field packet "certificate")
      return proof
    saveNative "producer" proofBefore root
    let decl := Json.mkObj [("name", Midas.Identity.name root),
      ("signature", ← signature expected levels workContext), ("source", toJson proofSource),
      ("requires", toJson (#[] : Array Json))]
    let proof := Json.mkObj [("format", toJson "midas-source-proof-v1"),
      ("request", ← checked (field packet "request")), ("purpose", toJson purpose),
      ("signature", sig), ("declarations", toJson #[decl])]
    return attachImports <| if polymorphic then proof.setObjVal! "assumptions" assumptions else proof
  let declarations ← ((← getEnv).constants.map₂.toList.filter (fun (n, _) => !before.contains n)).toArray.mapM (fun (_, c) => encodeDecl c)
  return attachImports <| Json.mkObj [("format", toJson "midas-lean-certificate-v1"),
    ("request", ← checked (field packet "request")), ("purpose", toJson purpose), ("signature", sig),
    ("declarations", toJson declarations)]

-- Binding statements are elaborated only in the producer. The second process
-- parses names and kernel-checks the closed expression without running the
-- statement. Neither process checks candidate proofs.
def checkBindings (packet : Json) (original : Environment) : CommandElabM Json := do
  let protocol ← checked (str packet "binding_protocol")
  unless protocol == "elaborated-bindings-v3" do
    throwError "unsupported binding protocol"
  let names ← check (packet.setObjVal! "mode" (toJson "names")) original
  let interface ← checked (field packet "interface")
  let certificate ← if interface == Json.null then pure Json.null else do
    let replay := (interface.getObjValAs? String "mode").toOption == some "replay"
    unless (← checked (str interface "purpose")) == "interface" &&
      ((← checked (str interface "mode")) == "produce" || replay) &&
      (← checked (field interface "candidate")) == Json.null &&
      ((← checked (field interface "certificate")) == Json.null || replay) &&
      (← checked (field interface "dependencies")) == toJson (#[] : Array Json) do
      throwError "binding preparation only accepts declarative interfaces"
    for key in ["source", "logical", "offset", "constant", "signature"] do
      unless (← checked (field interface key)) == (← checked (field packet key)) do
        throwError "binding interface scope mismatch"
    unless (interface.getObjValAs? String "statement_protocol").toOption == some "elaborated-statements-v1" do
      throwError "missing elaborated statement protocol"
    check interface original
  return Json.mkObj [("format", toJson "midas-binding-result-v2"),
    ("request", ← checked (field packet "request")),
    ("parameters", ← checked (field names "parameters")), ("certificate", certificate)]

-- Export audit starts from imports and processes the complete proposed source.
-- It checks every source declaration, including unselected unfinished work.
partial def finishSource : FrontendM Unit := do
  let done ← processCommand
  if (← getCommandState).messages.hasErrors then
    for message in (← getCommandState).messages.toList do
      IO.eprintln (← message.toString)
    throw (IO.userError "export source failed to elaborate")
  unless done do finishSource

-- Source exports also contain trusted local structure/class declarations. Rebuild
-- their inductive blocks in the fresh kernel before replaying dependent values.
-- Candidate fragments still use the narrower auditNew boundary above.
-- Structural recursion has a safe kernel definition and a separate partial
-- executable implementation. Only the former is logical evidence. Do not add
-- the latter to the replay environment: any logical reference to it must fail.
def sourceRuntimeImplementation (before env : Environment) (ci : ConstantInfo) : Bool := Id.run do
  let .defnInfo d := ci | return false
  unless d.safety == .partial do return false
  let some parent := Compiler.isUnsafeRecName? d.name | return false
  if before.contains parent || !Meta.recExt.isTagged env parent then return false
  let some (.defnInfo original) := env.find? parent | return false
  unless original.safety == .safe && original.type == d.type &&
    original.levelParams == d.levelParams do return false
  return d.all.all fun n =>
    match Compiler.isUnsafeRecName? n with
    | some p => match env.find? p with
      | some (.defnInfo v) => v.safety == .safe
      | _ => false
    | none => false

def auditSourceDeclarations (before : Environment) : CommandElabM Unit := do
  let after ← getEnv
  let source := after.constants.map₂.toList.filter (fun (n, _) => !before.contains n)
  for (_, ci) in source do
    if sourceRuntimeImplementation before after ci then auditConstant ci
  let mut pending := source.filter (fun (_, ci) => !sourceRuntimeImplementation before after ci)
  let mut replay := before
  for (_, ci) in pending do
    match ci with
    | .defnInfo d => unless d.safety == .safe do throwError "unsafe source declaration"
    | .thmInfo _ => pure ()
    | .inductInfo d => unless !d.isUnsafe do throwError "unsafe inductive"
    | .ctorInfo d => unless !d.isUnsafe do throwError "unsafe constructor"
    | .recInfo d => unless !d.isUnsafe do throwError "unsafe recursor"
    | _ => throwError "unsupported source declaration kind"
    auditConstant ci
  while !pending.isEmpty do
    let mut rest := []
    let mut progressed := false
    for (n, ci) in pending do
      if let some existing := replay.find? n then
        unless existing.type == ci.type do throwError "generated kernel declaration differs: {n}"
        progressed := true
      else
        let decl? ← match ci with
          | .defnInfo d => pure (some (Declaration.defnDecl d))
          | .thmInfo d => pure (some (Declaration.thmDecl d))
          | .inductInfo d => do
            let types ← d.all.mapM fun name => do
              let some (.inductInfo info) := after.find? name
                | throwError "missing mutual inductive"
              let ctors ← info.ctors.mapM fun ctor => do
                let some (.ctorInfo value) := after.find? ctor
                  | throwError "missing constructor"
                pure ({ name := ctor, type := value.type } : Constructor)
              pure ({ name, type := info.type, ctors } : InductiveType)
            pure (some (Declaration.inductDecl d.levelParams d.numParams types false))
          | _ => pure none
        if let some decl := decl? then
          match replay.addDeclCore 0 10000 decl none true with
          | .ok env =>
            replay := env
            progressed := true
          | .error _ => rest := (n, ci) :: rest
        else rest := (n, ci) :: rest
    unless progressed do throwError "source kernel replay failed or has undeclared dependencies"
    pending := rest

def auditSource (packet : Json) (before : Environment) : CommandElabM Json := do
  -- Anonymous is a prefix of every declaration name. Apply the same safe
  -- declaration/axiom audit and independent kernel replay to the whole source,
  -- including its non-target declarations, without a candidate-name restriction.
  auditSourceDeclarations before
  let targets ← checked ((← checked (field packet "targets")).getArr?)
  for target in targets do
    let n ← checked (readName (← checked (field target "constant")))
    let ci ← getConstInfo n
    unless (← signature ci.type ci.levelParams) == (← checked (field target "signature")) do
      throwError "export target signature mismatch: {n}"
    audit n
  return Json.mkObj [("format", toJson "midas-source-audit-v1"),
    ("request", ← checked (field packet "request")), ("targets", toJson targets)]

end Midas.Check

def main (args : List String) : IO UInt32 := do
  let [packetPath] := args | throw (IO.userError "expected checker packet")
  let packet ← IO.ofExcept (Midas.Binary.decode (← IO.FS.readBinFile packetPath))
  let source ← IO.ofExcept (Midas.Check.str packet "source")
  let logical ← IO.ofExcept (Midas.Check.str packet "logical")
  let offset ← IO.ofExcept (Midas.Check.nat packet "offset")
  let maxHeartbeat ← IO.ofExcept (Midas.Check.nat packet "max_heartbeat")
  unsafe enableInitializersExecution
  initSearchPath (← findSysroot)
  let ictx := Parser.mkInputContext source logical
  let (header, ps, messages) ← Parser.parseHeader ictx
  if (← IO.ofExcept (Midas.Check.str packet "mode")) == "audit" then
    if (Elab.HeaderSyntax.imports header).any (fun i => i.module == `MidasCheck) then
      throw (IO.userError "export cannot import the private verifier module")
  let moduleName := ((logical.dropEnd 5).toString.splitOn "/").foldl Name.str Name.anonymous
  let (env, messages) ← Midas.Check.restoreImports header messages ictx moduleName
  if messages.hasErrors then throw (IO.userError "source imports failed")
  Midas.Check.launchEnvironment.set (some env)
  let action : FrontendM Json := do
    if (← IO.ofExcept (Midas.Check.str packet "mode")) == "audit" then
      Midas.Check.finishSource
      return ← runCommandElabM (Midas.Check.auditSource packet env)
    let before ← Midas.Check.seek offset
    let original := (← getCommandState).env
    setCommandState before
    runCommandElabM do
      if (← Midas.Check.checked (Midas.Check.str packet "mode")) == "lookup" then
        unless (Midas.Check.str packet "format").toOption == some "midas-lookup-packet-v3" do
          throwError "unsupported lookup packet format"
        Midas.Check.lookup packet
      else if (← Midas.Check.checked (Midas.Check.str packet "mode")) == "bindings" then
        Midas.Check.checkBindings packet original
      else
        Midas.Check.check packet original
  try
    let (result, _) ← (action.run {inputCtx := ictx}).run {
      commandState := Command.mkState env messages
        (maxHeartbeats.set (({} : Options).setBool `Elab.async false) maxHeartbeat),
      parserState := ps, cmdPos := ps.pos }
    let artifacts ← Midas.Check.nativeOutputs.get
    let some path ← IO.getEnv "MIDAS_EVIDENCE_PATH"
      | throw (IO.userError "missing evidence channel")
    if artifacts.isEmpty then
      IO.FS.writeBinFile path (← IO.ofExcept (Midas.Binary.encode result))
    else
      let header ← IO.ofExcept (Midas.Binary.encode (Json.mkObj [("format", toJson "midas-native-declarations-v2"),
        ("proof", result), ("artifacts", toJson (artifacts.map fun (id, bytes) =>
          Json.mkObj [("id", toJson id), ("bytes", toJson bytes.size)]))]))
      let mut output := "MIDAS_NATIVE_V2\n".toUTF8
      for shift in [:8] do output := output.push (UInt8.ofNat (header.size / 256^shift % 256))
      output := output ++ header
      for (_, bytes) in artifacts do output := output ++ bytes
      IO.FS.writeBinFile path output
    return 0
  catch error =>
    let detail := error.toString
    if (detail.splitOn "certificate graph").length > 1 && (detail.splitOn "limit exceeded").length > 1 then
      IO.eprintln "MIDAS_EVIDENCE_LIMIT"
    else
      IO.eprintln "MIDAS_CHECK_REJECTED"
    IO.eprintln detail
    return 1
