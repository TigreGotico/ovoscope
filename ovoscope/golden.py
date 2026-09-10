"""Multi-engine golden-utterance evaluation.

Loads a skill's golden-utterance corpus (the ``test/end2end/
golden_utterances*.jsonl`` schema — one row per ``utterance``/``lang``/
expected intent) and runs it through several real OPM (OVOS Plugin
Manager) intent-matching pipeline plugins, WITHOUT booting a full
MiniCroft/message-bus stack.

Resolution is generic: a "fighter" is a pipeline **entry-point id** (the
same ids ``mycroft.conf``'s ``intents.pipeline`` lists and
``ovos-plugin-arena`` names competitors after) plus a confidence tier
and a config dict. Any installed OPM pipeline plugin that implements
the standard ``register_intent(message)`` / ``calc_intent(utterances,
lang, message)`` / ``match_<tier>(utterances, lang, message)`` trio
(``ovos_plugin_manager.templates.pipeline.ConfidenceMatcherPipeline``'s
own shape) is runnable by adding one entry to :data:`FIGHTERS` — no new
adapter class, no ovoscope change to support e.g. a newly-registered
fusion engine. :class:`EngineAdapter` exists only as an escape hatch
for a plugin whose registration/training needs genuinely don't fit
that generic shape (``M2VPrototypeAdapter`` is the one such case here —
see its docstring).

Because the plugin's own ``match_<tier>`` and ``calc_intent`` are
called directly, a fighter's score is the real shipping pipeline's
score, not a reimplementation — the runner and the pipeline compete
identically.

Engines are resolved lazily: a fighter whose entry point is not
installed (or, for ``m2v-prototype``, has no reachable embedding model)
produces an explicit ``unavailable`` scoreboard row with a **stable**
reason id (``not-installed`` / ``no-model`` / ``init-error``) — it is
never silently skipped and never counted as a pass.

Two things come out of one evaluation:

* a **scoreboard** (``competitor_id -> {total, matched, pct, failures,
  ...}``) used to gate a skill's own CI — the template-engine fighters
  (``padatious-medium``, ``padacioso-medium``, ``nebulento-medium``)
  are gating by default (100% of the corpus must match at their tier's
  confidence gate, ``core`` rows are 100% mandatory); ``m2v-prototype``
  is informational only until its confidence thresholds are calibrated
  fleet-wide and its scores come from a validated scorer (see the
  ``M2VPrototypeAdapter`` docstring);
* a stream of **prediction rows** shaped like ovos-plugin-arena's
  prediction contract (``docs/SPECIFICATION.md`` §3.2 on the
  ``OpenVoiceOS/ovos-plugin-arena`` repo — the canonical source; this
  module mirrors that shape without importing the arena package) so
  the same corpus run can be fed straight into the arena's benchmark
  pipeline. Written to ``predictions/<lang>/<competitor_id>.jsonl`` per
  §3.2's file layout. ``m2v-prototype`` rows are withheld from this
  output (see the TODO on :func:`write_predictions`).
"""
from __future__ import annotations

import dataclasses
import json
import time
from datetime import datetime, timezone
from importlib import metadata as importlib_metadata
from pathlib import Path
from typing import Any, Dict, FrozenSet, List, Optional, Tuple, Union

__all__ = [
    "GoldenRow",
    "PredictionRow",
    "load_golden_rows",
    "EngineAdapter",
    "FighterSpec",
    "FIGHTERS",
    "resolve_pipeline_plugin",
    "GenericOPMAdapter",
    "M2VPrototypeAdapter",
    "DEFAULT_ENGINES",
    "DEFAULT_GATING_ENGINES",
    "build_engine_intents",
    "build_engine_entities",
    "run_golden_suite",
    "assert_gates",
    "write_predictions",
]

RUNNER_VERSION = "ovoscope-golden-2"

# Stable `unavailable` reason ids — arena rows/consumers key off these,
# never off free-text messages.
REASON_NOT_INSTALLED = "not-installed"
REASON_NO_MODEL = "no-model"
REASON_INIT_ERROR = "init-error"


# ---------------------------------------------------------------------------
# Corpus rows
# ---------------------------------------------------------------------------
@dataclasses.dataclass(frozen=True)
class GoldenRow:
    """One golden-utterance corpus row.

    ``expected_intent`` is the canonical ``skill_id:IntentName`` id (or
    a bare ``IntentName`` when the caller only compares against a single
    skill); ``None`` marks an out-of-scope sample where the correct
    engine behaviour is *no match*. ``core`` marks a must-match row: it
    is required to match on every GATING engine regardless of that
    engine's aggregate threshold. ``provenance`` carries optional,
    tolerated-if-missing bookkeeping fields (``source_repo``,
    ``source_file``, ``intent_label_original``, ``intent_type``,
    ``intent_method``, ``needs_manual``, ``machine_generated``,
    ``required_vocab``, ``expected_messages``, ...).
    """
    utterance: str
    lang: str
    skill_id: str
    expected_intent: Optional[str]
    core: bool = False
    sample_id: Optional[str] = None
    provenance: Dict[str, Any] = dataclasses.field(default_factory=dict)

    def key(self) -> str:
        return self.sample_id or f"{self.skill_id}:{self.lang}:{self.utterance}"


def load_golden_rows(path: Union[str, Path]) -> List[GoldenRow]:
    """Load golden-utterance rows from a JSONL file.

    Accepts the legacy ``intent_label`` key as an alias for
    ``expected_intent`` (the schema named in the original golden-
    utterance convention), and tolerates any missing provenance field.
    """
    rows: List[GoldenRow] = []
    known = {"utterance", "lang", "skill_id", "expected_intent",
             "intent_label", "core", "sample_id"}
    for line in Path(path).read_text(encoding="utf-8").splitlines():
        line = line.strip()
        if not line or line.startswith("#"):
            continue
        data = json.loads(line)
        expected = data.get("expected_intent", data.get("intent_label"))
        provenance = {k: v for k, v in data.items() if k not in known}
        rows.append(GoldenRow(
            utterance=data["utterance"],
            lang=data["lang"],
            skill_id=data["skill_id"],
            expected_intent=expected,
            core=bool(data.get("core", False)),
            sample_id=data.get("sample_id"),
            provenance=provenance,
        ))
    return rows


# ---------------------------------------------------------------------------
# Arena prediction-row mirror (canonical: ovos-plugin-arena
# docs/SPECIFICATION.md §3.2, runner/schema.py PredictionRow). Mirrored
# here, not imported, so this module has no hard dependency on the
# arena repo.
# ---------------------------------------------------------------------------
@dataclasses.dataclass
class PredictionRow:
    competitor_id: str
    sample_id: str
    dataset_id: str
    lang: str
    plugin_id: str
    plugin_version: str
    prediction: Optional[str]
    runner_version: str
    created_at: str
    modality: str = "intent"
    # Intent-modality columns (§3.2 "Intent").
    utterance: str = ""
    reference_intent: Optional[str] = None
    reference_slots: Optional[dict] = None
    predicted_slots: Optional[dict] = None
    exact_match: bool = False
    confidence: float = 0.0
    bucket: Optional[str] = None
    latency_ms: float = 0.0
    # Reproducibility (SHOULD be set).
    dataset_revision: Optional[str] = None
    schema_version: int = 2

    def to_dict(self) -> dict:
        return dataclasses.asdict(self)


def write_predictions(predictions: List[PredictionRow], out_dir: Union[str, Path]) -> List[Path]:
    """Write predictions to ``<out_dir>/predictions/<lang>/<competitor_id>.jsonl``
    per the §3.2 file layout, one JSON object per line.

    ``m2v-prototype`` rows are deliberately EXCLUDED: today's adapter is
    a stand-in centroid approximation, not the real pipeline's own
    scoring (see :class:`M2VPrototypeAdapter`), so publishing it would
    poison the zero-shot fighter's dataset with a score no shipping
    pipeline actually produces. It stays in the scoreboard as an
    indicative CI display percentage only. TODO: once
    ``ovos-m2v-pipeline`` exposes a standalone ``PrototypeScorer`` (the
    real per-sample max-cosine store, not a per-intent centroid), switch
    the adapter to it and drop this exclusion.
    """
    base = Path(out_dir) / "predictions"
    grouped: Dict[Tuple[str, str], List[PredictionRow]] = {}
    for p in predictions:
        if p.competitor_id == "m2v-prototype":
            continue
        grouped.setdefault((p.lang, p.competitor_id), []).append(p)
    written = []
    for (lang, competitor_id), rows in grouped.items():
        path = base / lang / f"{competitor_id}.jsonl"
        path.parent.mkdir(parents=True, exist_ok=True)
        with path.open("w", encoding="utf-8") as fh:
            for row in rows:
                fh.write(json.dumps(row.to_dict(), ensure_ascii=False) + "\n")
        written.append(path)
    return written


# ---------------------------------------------------------------------------
# Engine adapters
# ---------------------------------------------------------------------------
class EngineAdapter:
    """Escape hatch for a fighter whose plugin doesn't fit the generic
    OPM ``register_intent``/``calc_intent``/``match_<tier>`` shape (see
    module docstring). Most fighters need no subclass at all —
    :class:`GenericOPMAdapter` plus a :class:`FighterSpec` entry is the
    normal, generic resolution path.
    """
    competitor_id: str = "unknown"
    plugin_id: str = "unknown"

    def plugin_version(self) -> str:
        return "unknown"

    def available(self) -> Tuple[bool, Optional[str]]:
        """Return ``(True, None)`` or ``(False, stable_reason_id)``."""
        raise NotImplementedError

    def build(self, intents: Dict[str, List[str]], *, skill_id: str, lang: str,
              entities: Optional[Dict[str, List[str]]] = None) -> Any:
        """Instantiate+train a container for one (skill_id, lang) group.
        ``intents`` maps intent name -> list of template/example lines.
        ``entities`` maps ``{name}`` slot name -> its registered value
        set (see :func:`build_engine_entities`) — omitting it leaves a
        template's ``{name}`` slot an unconstrained wildcard, which the
        real shipping pipeline never does once that entity is
        registered."""
        raise NotImplementedError

    def match(self, container: Any, utterance: str, lang: str
              ) -> Tuple[Optional[str], float, float, Optional[dict]]:
        """Return ``(intent_name_or_None, confidence, latency_ms,
        matched_slots_or_None)``."""
        raise NotImplementedError


def resolve_pipeline_plugin(entry_point_id: str):
    """Resolve an installed OPM pipeline plugin class by its
    ``ovos_plugin_manager.pipeline`` entry-point id (e.g.
    ``"ovos-padatious-pipeline-plugin"``). Returns ``None`` if not
    installed."""
    try:
        from ovos_plugin_manager.pipeline import find_pipeline_plugins
    except ImportError:
        return None
    return find_pipeline_plugins().get(entry_point_id)


def _extract_name_conf(result: Any) -> Tuple[Optional[str], float]:
    """``calc_intent`` returns objects with ``name``/``conf`` attributes
    on every engine (padatious' ``MatchData``, padacioso's
    ``PadaciosoIntent``, nebulento's ``NebulentoIntent``). Their ``.get``
    method (when present) is a SLOT accessor over the matched entities
    (``MatchData.get(key) -> self.matches.get(key)``), NOT a dict-style
    getter over ``name``/``conf`` — branching on ``hasattr(result,
    "get")`` silently returns ``None``/``0.0`` for every one of them.
    Always read via ``getattr``."""
    if result is None:
        return None, 0.0
    return getattr(result, "name", None), float(getattr(result, "conf", 0.0) or 0.0)


def _extract_slots(result: Any) -> Optional[dict]:
    """Pull the matched-entity dict off a ``calc_intent`` result
    (``MatchData.matches`` / equivalent on padacioso and nebulento)."""
    if result is None:
        return None
    slots = getattr(result, "matches", None)
    return dict(slots) if slots else None


@dataclasses.dataclass(frozen=True)
class FighterSpec:
    """One arena registry fighter: an entry-point id, the confidence
    tier it competes at, and the exact plugin config that tier implies.
    Matching these fields precisely is what makes a ``competitor_id``
    correct — any deviation is a different, unregistered fighter."""
    competitor_id: str
    entry_point: str
    tier: str  # "high" | "medium" | "low"
    config: Dict[str, Any]
    plugin_id: str  # importlib.metadata distribution name, for plugin_version


# Registry fighter configs (verified against the arena registry).
# NOTE: this dict is DATA, not the resolution mechanism — a new fighter
# for any OPM-conformant pipeline plugin (including a fusion engine
# ovoscope has never seen) is one entry here, no code change.
FIGHTERS: Dict[str, FighterSpec] = {
    "padatious-medium": FighterSpec(
        competitor_id="padatious-medium",
        entry_point="ovos-padatious-pipeline-plugin",
        tier="medium",
        config={"conf_high": 0.95, "conf_med": 0.8, "conf_low": 0.5,
                "domain_engine": False, "cast_to_ascii": False,
                "stem": False, "disable_padaos": False,
                "instant_train": False},
        plugin_id="ovos-padatious",
    ),
    "padacioso-medium": FighterSpec(
        competitor_id="padacioso-medium",
        entry_point="ovos-padacioso-pipeline-plugin",
        tier="medium",
        config={"conf_high": 0.95, "conf_med": 0.8, "conf_low": 0.5,
                "fuzz": False},
        plugin_id="padacioso",
    ),
    "nebulento-medium": FighterSpec(
        competitor_id="nebulento-medium",
        entry_point="ovos-nebulento-pipeline-plugin",
        tier="medium",
        config={"conf_high": 0.95, "conf_med": 0.8, "conf_low": 0.5},
        plugin_id="nebulento",
    ),
}


class GenericOPMAdapter(EngineAdapter):
    """Drives ANY installed OPM ``ConfidenceMatcherPipeline``-shaped
    plugin generically, by entry-point id, using its own
    ``register_intent`` / ``calc_intent`` / ``match_<tier>`` methods —
    the exact same call path the plugin ships and the real intent
    service uses. No bus wiring is needed for those calls (they are
    plain methods); a throwaway ``FakeBus`` only satisfies the
    constructor.

    A ``prediction`` is only ever the plugin's own ``match_<tier>``
    verdict (``None`` when the plugin's raw confidence doesn't clear
    its own tier gate) — never a value guessed by the runner. Raw
    ``confidence`` is read separately via ``calc_intent`` so a
    below-gate row can still report *how close* it was, matching
    §3.2's "no match scores as a null prediction" convention.
    """

    def __init__(self, spec: FighterSpec):
        self.spec = spec
        self.competitor_id = spec.competitor_id
        self.plugin_id = spec.plugin_id
        self._cls = None
        self._probe: Any = None

    def plugin_version(self) -> str:
        try:
            return importlib_metadata.version(self.plugin_id)
        except importlib_metadata.PackageNotFoundError:
            return "unknown"

    def available(self):
        self._cls = resolve_pipeline_plugin(self.spec.entry_point)
        if self._cls is None:
            return False, REASON_NOT_INSTALLED
        try:
            from ovos_utils.fakebus import FakeBus
            # The init-error check needs a real instance; keep it as the
            # probe rather than discarding it — the first build() call
            # reuses it instead of constructing a second one.
            self._probe = self._cls(bus=FakeBus(), config=dict(self.spec.config))
        except Exception:  # pragma: no cover - defensive
            return False, REASON_INIT_ERROR
        return True, None

    def build(self, intents, *, skill_id, lang, entities=None):
        from ovos_spec_tools.message import Message
        from ovos_utils.fakebus import FakeBus
        if self._probe is not None:
            plugin, self._probe = self._probe, None
        else:
            plugin = self._cls(bus=FakeBus(), config=dict(self.spec.config))
        for name, lines in (entities or {}).items():
            if not lines:
                continue
            msg = Message(f"{self.plugin_id}:register_entity",
                          {"name": name, "samples": lines, "lang": lang},
                          {"skill_id": skill_id})
            plugin.register_entity(msg)
        for name, lines in intents.items():
            if not lines:
                continue
            msg = Message(f"{self.plugin_id}:register_intent",
                          {"name": name, "samples": lines, "lang": lang},
                          {"skill_id": skill_id})
            plugin.register_intent(msg)
        if hasattr(plugin, "train"):
            plugin.train(Message("mycroft.skills.train", {}, {}))
        return plugin

    def match(self, container, utterance, lang):
        from ovos_spec_tools.message import Message
        plugin = container
        msg = Message("recognizer_loop:utterance",
                      {"utterances": [utterance], "lang": lang}, {})
        t0 = time.monotonic()
        tier_method = getattr(plugin, f"match_{self.spec.tier}")
        verdict = tier_method([utterance], lang, msg)
        calc = plugin.calc_intent([utterance], lang, msg)
        dt = (time.monotonic() - t0) * 1000
        _, conf = _extract_name_conf(calc)
        slots = _extract_slots(calc)
        name = verdict.match_type if verdict is not None else None
        return name, conf, dt, slots


class M2VPrototypeAdapter(EngineAdapter):
    """Model2Vec zero-shot fighter (``m2v-prototype``) — CI-DISPLAY-ONLY.

    ``ovos-m2v-pipeline`` does not currently expose a standalone,
    bus-free scoring path: its real ``Model2VecPrototypePipeline`` keeps
    every registered sample as its own vector and scores a query by
    max-per-label cosine, plus bounded template/entity expansion before
    embedding — none of which this adapter reproduces. This adapter
    instead averages each intent's example embeddings into ONE centroid
    per intent, which is a materially different (and generally weaker)
    algorithm than the real pipeline's, so its score MUST NOT be
    published as if it were the real fighter's:

    * :func:`run_golden_suite` reports it in the scoreboard as an
      indicative percentage, never gating by default;
    * :func:`write_predictions` excludes ``m2v-prototype`` rows
      entirely.

    TODO: ``ovos-m2v-pipeline`` is expected to expose a standalone
    ``PrototypeScorer`` (importing the real per-sample vector store).
    Once that lands, replace this centroid approximation with it,
    remove the write_predictions exclusion, and this adapter becomes a
    normal, publishable fighter like the template engines.
    """
    competitor_id = "m2v-prototype"
    plugin_id = "ovos-m2v-pipeline"

    # Registry config (informational-only fighter; not yet gating).
    conf_high = 0.6
    conf_medium = 0.45
    conf_low = 0.3

    def __init__(self, model_name: str = "minishlab/potion-base-8M",
                threshold: Optional[float] = None):
        self.model_name = model_name
        self.threshold = self.conf_medium if threshold is None else threshold
        self._model = None

    def plugin_version(self) -> str:
        try:
            return importlib_metadata.version(self.plugin_id)
        except importlib_metadata.PackageNotFoundError:
            return "unknown"

    def available(self):
        if self._model is not None:
            return True, None
        try:
            from model2vec import StaticModel
        except ImportError:
            return False, REASON_NOT_INSTALLED
        try:
            self._model = StaticModel.from_pretrained(self.model_name)
        except Exception:
            return False, REASON_NO_MODEL
        return True, None

    def build(self, intents, *, skill_id, lang, entities=None):
        import numpy as np
        ok, _ = self.available()
        if not ok:
            return None
        prototypes = {}
        for name, lines in intents.items():
            if not lines:
                continue
            vecs = self._model.encode(lines)
            prototypes[name] = np.mean(vecs, axis=0)
        return prototypes

    def match(self, container, utterance, lang):
        if not container:
            return None, 0.0, 0.0
        import numpy as np
        t0 = time.monotonic()
        vec = self._model.encode([utterance])[0]
        best_name, best_score = None, -1.0
        for name, proto in container.items():
            denom = (np.linalg.norm(vec) * np.linalg.norm(proto)) or 1e-9
            score = float(np.dot(vec, proto) / denom)
            if score > best_score:
                best_name, best_score = name, score
        dt = (time.monotonic() - t0) * 1000
        if best_score < self.threshold:
            return None, best_score, dt, None
        return best_name, best_score, dt, None


def _make_default_engines() -> Dict[str, EngineAdapter]:
    engines: Dict[str, EngineAdapter] = {
        cid: GenericOPMAdapter(spec) for cid, spec in FIGHTERS.items()
    }
    engines["m2v-prototype"] = M2VPrototypeAdapter()
    return engines


DEFAULT_ENGINES: Dict[str, EngineAdapter] = _make_default_engines()

# Template engines gate CI by default; m2v-prototype stays informational
# until its confidence thresholds are calibrated AND it scores through
# the real pipeline (see M2VPrototypeAdapter) — flip fleet-wide via
# run_golden_suite(m2v_gating=True), never per-skill.
DEFAULT_GATING_ENGINES: FrozenSet[str] = frozenset(FIGHTERS.keys())


# ---------------------------------------------------------------------------
# Building engine training data from a skill's own resource files
# ---------------------------------------------------------------------------
def _read_lines(path: Path) -> List[str]:
    return [
        l.strip() for l in path.read_text(encoding="utf-8").splitlines()
        if l.strip() and not l.strip().startswith("#")
    ]


def build_engine_entities(resources_dir: Union[str, Path], lang: str
                           ) -> Dict[str, List[str]]:
    """Build ``{entity_name: [value lines]}`` from a skill's
    ``locale/<lang>/*.entity`` files.

    ``.entity`` files carry the value set for a ``{name}`` slot
    referenced inside an ``.intent`` template — this is what the real
    padatious/padacioso pipelines register via
    ``register_intent``'s companion ``register_entity`` so the slot is
    a constrained lookup, not a wildcard, at match time. This is
    distinct from ``.voc`` files, which only feed ``<name>`` keyword
    inlining (see :func:`build_engine_intents`).
    """
    lang_dir = Path(resources_dir) / lang
    if not lang_dir.is_dir():
        return {}
    return {
        entity_file.stem: _read_lines(entity_file)
        for entity_file in lang_dir.glob("*.entity")
    }


def build_engine_intents(resources_dir: Union[str, Path], lang: str
                          ) -> Dict[str, List[str]]:
    """Build ``{intent_name: [template lines]}`` from a skill's
    ``locale/<lang>/*.intent`` and ``*.voc`` files.

    ``<name>`` keyword references are inlined from sibling ``.voc``
    files via ``ovos_spec_tools.expansion.inline_keywords``. ``expand``
    only expands that ``<name>`` keyword (and OVOS-INTENT-1's optional-
    group) syntax — ``{name}`` slots are left as opaque tokens in the
    returned template lines, exactly as the real pipeline plugins see
    them; a slot is only constrained at match time once its values are
    registered as an entity (see :func:`build_engine_entities` — the
    shipping pipeline rejects an unconstrained ``{name}`` guess against
    a registered entity it doesn't recognise, so callers MUST register
    entities too, not just intents, to reproduce that behaviour).
    Requires the ``ovos-spec-tools`` package (already a runtime
    dependency of this repo's ``audio`` extra); raises ``ImportError``
    with a clear message if it is missing.
    """
    try:
        from ovos_spec_tools.expansion import expand, inline_keywords
    except ImportError as e:
        raise ImportError(
            "build_engine_intents requires ovos-spec-tools "
            "(pip install 'ovoscope[engines]')") from e

    lang_dir = Path(resources_dir) / lang
    if not lang_dir.is_dir():
        return {}

    vocab: Dict[str, List[str]] = {
        voc_file.stem: _read_lines(voc_file)
        for voc_file in lang_dir.glob("*.voc")
    }

    intents: Dict[str, List[str]] = {}
    for intent_file in lang_dir.glob("*.intent"):
        templates = _read_lines(intent_file)
        lines: List[str] = []
        for template in templates:
            inlined = inline_keywords(template, vocab)
            lines.extend(expand(inlined, vocab))
        intents[intent_file.stem] = lines
    return intents


# ---------------------------------------------------------------------------
# Evaluation
# ---------------------------------------------------------------------------
def _short_intent(name: Optional[str]) -> Optional[str]:
    """Fold a ``skill_id:Intent`` or ``Intent.intent`` id to the bare
    intent name an engine container was built with."""
    if not name:
        return name
    name = name.split(":")[-1]
    if name.endswith(".intent"):
        name = name[:-len(".intent")]
    return name


def run_golden_suite(
        rows: List[GoldenRow],
        intents_by_group: Dict[Tuple[str, str], Dict[str, List[str]]],
        *,
        entities_by_group: Optional[Dict[Tuple[str, str], Dict[str, List[str]]]] = None,
        engines: Optional[Dict[str, EngineAdapter]] = None,
        gating_engines: FrozenSet[str] = DEFAULT_GATING_ENGINES,
        m2v_gating: bool = False,
        m2v_threshold: Optional[float] = None,
        dataset_id: str = "golden-utterances",
        dataset_revision: Optional[str] = None,
) -> Tuple[Dict[str, dict], List[PredictionRow]]:
    """Evaluate every golden row against every configured engine.

    ``intents_by_group`` maps ``(skill_id, lang) -> {intent: [lines]}``
    (build with :func:`build_engine_intents` per group, or supply your
    own for a toy/synthetic corpus). ``entities_by_group`` maps the same
    keys to ``{name: [values]}`` (build with :func:`build_engine_entities`)
    — a ``{name}`` slot referenced by a template is only constrained at
    match time once its entity is registered here; omitting it leaves
    every such slot an unconstrained wildcard, unlike the real pipeline.

    Callers passing ``engines=None`` (the default) get FRESH adapter
    instances built from :data:`FIGHTERS`/:class:`M2VPrototypeAdapter`
    for this call, never the shared :data:`DEFAULT_ENGINES` objects —
    ``m2v_threshold`` and per-plugin caches (``_cls``/``_model``) would
    otherwise permanently reconfigure the process-wide defaults for
    every later, unrelated call.

    Returns ``(scoreboard, predictions)``:

    * ``scoreboard``: ``{competitor_id: {total, matched, pct,
      unavailable, reason, core_total, core_matched, gate_passed,
      gate_reason, failures: [...]}}``. An unavailable GATING engine
      fails its gate (never a silent pass); an unavailable
      m2v-prototype does not, because it is informational by default.
    * ``predictions``: one :class:`PredictionRow` per (engine, row) that
      actually ran (unavailable engines emit no prediction rows — there
      is nothing to score). ``m2v-prototype`` rows are included here
      for completeness but are dropped by :func:`write_predictions`.
    """
    engines = dict(engines) if engines is not None else _make_default_engines()
    entities_by_group = entities_by_group or {}
    now = datetime.now(timezone.utc).isoformat()

    scoreboard: Dict[str, dict] = {}
    predictions: List[PredictionRow] = []

    for engine_id, adapter in engines.items():
        if engine_id == "m2v-prototype" and m2v_threshold is not None:
            adapter.threshold = m2v_threshold  # type: ignore[attr-defined]

        ok, reason = adapter.available()
        entry = {
            "total": len(rows), "matched": 0, "pct": 0.0,
            "unavailable": not ok, "reason": reason,
            "core_total": 0, "core_matched": 0,
            "failures": [],
        }
        is_gating = engine_id in gating_engines or (engine_id == "m2v-prototype" and m2v_gating)

        if not ok:
            entry["gate_passed"] = not is_gating
            entry["gate_reason"] = reason if is_gating else None
            scoreboard[engine_id] = entry
            continue

        containers = {
            group: adapter.build(group_intents, skill_id=group[0], lang=group[1],
                                  entities=entities_by_group.get(group))
            for group, group_intents in intents_by_group.items()
        }

        for row in rows:
            group = (row.skill_id, row.lang)
            container = containers.get(group)
            if container is None:
                predicted, conf, latency, slots = None, 0.0, 0.0, None
            else:
                predicted, conf, latency, slots = adapter.match(container, row.utterance, row.lang)
            expected_short = _short_intent(row.expected_intent)
            matched = _short_intent(predicted) == expected_short
            if matched:
                entry["matched"] += 1
            else:
                entry["failures"].append({
                    "utterance": row.utterance, "lang": row.lang,
                    "expected": row.expected_intent, "got": predicted,
                    "confidence": conf, "core": row.core,
                })
            if row.core:
                entry["core_total"] += 1
                if matched:
                    entry["core_matched"] += 1

            predictions.append(PredictionRow(
                competitor_id=adapter.competitor_id,
                sample_id=row.key(),
                dataset_id=dataset_id,
                dataset_revision=dataset_revision,
                lang=row.lang,
                plugin_id=adapter.plugin_id,
                plugin_version=adapter.plugin_version(),
                prediction=predicted,
                runner_version=RUNNER_VERSION,
                created_at=now,
                utterance=row.utterance,
                reference_intent=row.expected_intent,
                reference_slots=None,
                predicted_slots=slots,
                exact_match=matched,
                confidence=conf,
                bucket=None,
                latency_ms=latency,
            ))

        entry["pct"] = (entry["matched"] / entry["total"]) if entry["total"] else 1.0
        core_ok = entry["core_total"] == entry["core_matched"]
        threshold = 1.0
        aggregate_ok = entry["pct"] >= threshold
        if is_gating:
            entry["gate_passed"] = core_ok and aggregate_ok
            entry["gate_reason"] = None if entry["gate_passed"] else (
                "core row(s) missed" if not core_ok else
                f"pct {entry['pct']:.2%} below threshold {threshold:.0%}")
        else:
            entry["gate_passed"] = True
            entry["gate_reason"] = None
        scoreboard[engine_id] = entry

    return scoreboard, predictions


def assert_gates(scoreboard: Dict[str, dict]) -> None:
    """Raise ``AssertionError`` listing every engine whose gate failed.

    Informational engines (``gate_passed`` already forced ``True`` by
    :func:`run_golden_suite`) never trigger this.
    """
    failed = {eid: entry for eid, entry in scoreboard.items()
              if not entry.get("gate_passed", True)}
    if not failed:
        return
    lines = []
    for eid, entry in failed.items():
        if entry.get("unavailable"):
            lines.append(f"{eid}: UNAVAILABLE ({entry.get('reason')})")
            continue
        lines.append(f"{eid}: {entry['matched']}/{entry['total']} "
                      f"({entry['pct']:.1%}), core "
                      f"{entry['core_matched']}/{entry['core_total']} — "
                      f"{entry.get('gate_reason')}")
        for f in entry["failures"][:10]:
            lines.append(f"    {f['lang']} {f['utterance']!r} -> "
                         f"expected {f['expected']!r}, got {f['got']!r}")
    raise AssertionError("golden-utterance gate failure:\n" + "\n".join(lines))
