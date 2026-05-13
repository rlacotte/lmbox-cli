"""Schema linter — catch common JSON Schema design foot-guns.

Goal : when an integrator writes a manifest with a `json_schema`
output contract, surface the schema-design mistakes that will
hurt model compliance BEFORE the agent ships. The model can't tell
you a field is ambiguous; the linter can.

Checks (current set)
────────────────────
  ERROR    — schema is structurally broken (will validate nothing
             usefully).
  WARNING  — model will probably miscomply (missing descriptions,
             unbounded strings, additionalProperties not set).
  INFO     — stylistic recommendations (consistent casing, enum
             cardinality, etc.).

Severity model is *prescriptive* : a WARNING fails `lmbox agent
lint-schema --strict` but not the default invocation. ERRORs always
fail.

Adding a check
──────────────
Each check is a pure function `Schema → Iterable[LintIssue]`.
Register it in `_ALL_CHECKS` below. Keep them small and focused —
the goal is to surface ONE concrete actionable issue per check.
"""

from __future__ import annotations

from collections.abc import Iterable, Iterator
from dataclasses import dataclass
from enum import Enum


class LintLevel(str, Enum):
    ERROR = "error"
    WARNING = "warning"
    INFO = "info"


@dataclass(frozen=True)
class LintIssue:
    level: LintLevel
    path: str            # dotted path inside the schema, "" for root
    rule: str            # short identifier, e.g. "missing_description"
    message: str         # human-readable advice


def lint_schema(schema: dict) -> list[LintIssue]:
    """Run every check on `schema` and return a flat sorted list."""
    issues: list[LintIssue] = []
    for check in _ALL_CHECKS:
        issues.extend(check(schema))
    level_order = {LintLevel.ERROR: 0, LintLevel.WARNING: 1, LintLevel.INFO: 2}
    issues.sort(key=lambda i: (level_order[i.level], i.path, i.rule))
    return issues


# ─── Checks ──────────────────────────────────────────────────────


def _check_top_level_type(schema: dict) -> Iterator[LintIssue]:
    """The root schema MUST be an object — otherwise the model has
    nowhere to write structured fields and the LLM backends don't
    know how to encode the response_format directive."""
    if schema.get("type") != "object":
        yield LintIssue(
            level=LintLevel.ERROR,
            path="",
            rule="root_must_be_object",
            message="Le schéma racine doit être de type 'object'. "
            "OpenAI / vLLM / Ollama exigent un schéma JSON Schema avec "
            "type=object au sommet pour activer response_format=json_schema.",
        )


def _check_additional_properties(schema: dict) -> Iterator[LintIssue]:
    """Every `object` node should set `additionalProperties: false`.
    Without it, small models tend to add extraneous fields (because
    "more = better" in their training distribution)."""
    for path, node in _walk_objects(schema):
        if "additionalProperties" not in node:
            yield LintIssue(
                level=LintLevel.WARNING,
                path=path,
                rule="missing_additional_properties",
                message="`additionalProperties` non précisé : "
                "ajouter `additionalProperties: false` pour empêcher "
                "le modèle d'inventer des champs hors schéma.",
            )
        elif node.get("additionalProperties") is True:
            yield LintIssue(
                level=LintLevel.WARNING,
                path=path,
                rule="permissive_additional_properties",
                message="`additionalProperties: true` autorise le modèle à "
                "inventer des champs. Préférer `false` ou un sous-schéma "
                "explicite.",
            )


def _check_descriptions(schema: dict) -> Iterator[LintIssue]:
    """Every named property should carry a `description`. The model
    sees the schema and uses descriptions as in-context guidance —
    no description = no guidance = worse compliance."""
    for path, node in _walk_objects(schema):
        for name, sub in (node.get("properties") or {}).items():
            field_path = f"{path}.properties.{name}" if path else f"properties.{name}"
            if not isinstance(sub, dict):
                continue
            if not sub.get("description"):
                yield LintIssue(
                    level=LintLevel.WARNING,
                    path=field_path,
                    rule="missing_description",
                    message=f"Le champ `{name}` n'a pas de `description`. "
                    "Ajouter une phrase courte (≤ 120 caractères) explicitant "
                    "ce que le modèle doit y mettre.",
                )


def _check_unbounded_strings(schema: dict) -> Iterator[LintIssue]:
    """A string property without `maxLength` is a token-bloat invitation
    — small models can run away into a 4 000-char paragraph for what
    should be a one-line label."""
    for path, node in _walk_all(schema):
        if node.get("type") == "string" and "maxLength" not in node and "enum" not in node:
            yield LintIssue(
                level=LintLevel.INFO,
                path=path,
                rule="unbounded_string",
                message="Champ string sans `maxLength` : risque que le modèle "
                "génère une réponse trop longue. Préciser une borne "
                "(ex: `maxLength: 200`).",
            )


def _check_enum_cardinality(schema: dict) -> Iterator[LintIssue]:
    """Enums with >20 values lose the model. Better split into a
    free-form field + a separate classifier prompt."""
    for path, node in _walk_all(schema):
        enum = node.get("enum")
        if isinstance(enum, list) and len(enum) > 20:
            yield LintIssue(
                level=LintLevel.WARNING,
                path=path,
                rule="oversized_enum",
                message=f"Enum à {len(enum)} valeurs : au-delà de 20, les "
                "petits modèles perdent la liste. Réduire ou remplacer par "
                "un champ libre + un classifier.",
            )


def _check_required_consistency(schema: dict) -> Iterator[LintIssue]:
    """Every `required` name must exist in `properties` — otherwise
    the schema is self-inconsistent and validation will always fail."""
    for path, node in _walk_objects(schema):
        required = node.get("required") or []
        props = set((node.get("properties") or {}).keys())
        for req in required:
            if req not in props:
                yield LintIssue(
                    level=LintLevel.ERROR,
                    path=path,
                    rule="required_missing_in_properties",
                    message=f"Le champ `{req}` est dans `required` mais "
                    "absent de `properties`. Aucune sortie ne pourra valider.",
                )


def _check_array_items(schema: dict) -> Iterator[LintIssue]:
    """An `array` without `items` accepts anything, which defeats the
    purpose of the structured output."""
    for path, node in _walk_all(schema):
        if node.get("type") == "array" and "items" not in node:
            yield LintIssue(
                level=LintLevel.WARNING,
                path=path,
                rule="unspecified_array_items",
                message="`array` sans `items` — le modèle peut mettre "
                "n'importe quel type dedans. Préciser `items: { type: ... }`.",
            )


_ALL_CHECKS = (
    _check_top_level_type,
    _check_additional_properties,
    _check_descriptions,
    _check_unbounded_strings,
    _check_enum_cardinality,
    _check_required_consistency,
    _check_array_items,
)


# ─── Traversal helpers ───────────────────────────────────────────


def _walk_objects(schema: dict) -> Iterable[tuple[str, dict]]:
    """Yield (path, node) for every nested object subschema."""
    yield from _walk_filtered(schema, "", lambda n: n.get("type") == "object")


def _walk_all(schema: dict) -> Iterable[tuple[str, dict]]:
    """Yield (path, node) for every dict subschema."""
    yield from _walk_filtered(schema, "", lambda _n: True)


def _walk_filtered(node, path, predicate):
    """Depth-first walk through `properties`, `items`, `oneOf`,
    `anyOf`, `allOf` — covering the structural surface most agent
    schemas use. We intentionally skip exotic constructs ($ref,
    if/then/else) to keep linter logic small; partner schemas can
    work around with explicit checks."""
    if not isinstance(node, dict):
        return
    if predicate(node):
        yield path, node
    for k, v in (node.get("properties") or {}).items():
        sub_path = f"{path}.properties.{k}" if path else f"properties.{k}"
        yield from _walk_filtered(v, sub_path, predicate)
    items = node.get("items")
    if isinstance(items, dict):
        yield from _walk_filtered(items, f"{path}.items" if path else "items", predicate)
    for combinator in ("oneOf", "anyOf", "allOf"):
        for idx, branch in enumerate(node.get(combinator) or []):
            yield from _walk_filtered(
                branch,
                f"{path}.{combinator}.{idx}" if path else f"{combinator}.{idx}",
                predicate,
            )
